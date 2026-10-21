# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Boolean, DateTime,
    UniqueConstraint, Index, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_SQLITE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'sre_sentinel.db')}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL).strip()

# 確保 SQLite 連線使用相對/絕對正確格式
if DATABASE_URL.startswith("sqlite:///"):
    db_file_path = DATABASE_URL.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(os.path.abspath(db_file_path)), exist_ok=True)
    connect_args = {"check_same_thread": False}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
    
    # 為 SQLite 自動開啟高並發 WAL 模式
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()
else:
    # MySQL / PostgreSQL 連線支援
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=3600
    )

SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
SessionScoped = scoped_session(SessionFactory)
Base = declarative_base()


# ============================================================================
# ORM Models (跨 SQLite / MySQL 100% 相容欄位)
# ============================================================================

class Project(Base):
    __tablename__ = "projects"

    service_name = Column(String(100), primary_key=True)
    project_path = Column(String(255), default="")
    project_id = Column(Integer, nullable=True)
    branch = Column(String(100), default="main")
    description = Column(Text, default="")
    log_field = Column(String(100), default="")
    log_value = Column(String(100), default="")
    notification_emails = Column(Text, default="")
    email_alert_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_name": self.service_name,
            "project_path": self.project_path or "",
            "project_id": self.project_id,
            "branch": self.branch or "main",
            "description": self.description or "",
            "log_field": self.log_field or "",
            "log_value": self.log_value or "",
            "notification_emails": self.notification_emails or "",
            "email_alert_enabled": self.email_alert_enabled
        }


class Issue(Base):
    __tablename__ = "issues"

    fingerprint = Column(String(64), primary_key=True)
    service_name = Column(String(100), index=True, nullable=False)
    error_type = Column(String(255), default="UnknownError")
    error_location = Column(String(255), default="unknown:0")
    first_seen_date = Column(String(10), default="")
    last_seen_date = Column(String(10), default="")
    consecutive_days = Column(Integer, default=1)
    today_count = Column(Integer, default=0)
    yesterday_count = Column(Integer, default=0)
    total_occurrences = Column(Integer, default=0)
    status = Column(String(20), default="new")  # new, ongoing, acknowledged, resolved
    acknowledge_note = Column(Text, nullable=True)
    acknowledged_at = Column(String(30), nullable=True)
    cached_analysis_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class IssueDailyHistory(Base):
    __tablename__ = "issue_daily_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fingerprint = Column(String(64), index=True, nullable=False)
    date = Column(String(10), index=True, nullable=False)
    count = Column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("fingerprint", "date", name="uq_fp_date"),
    )


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_name = Column(String(100), index=True, nullable=False)
    report_date = Column(String(10), index=True, nullable=False)
    severity = Column(String(20), default="Normal")
    title = Column(String(255), default="")
    root_cause_summary = Column(Text, default="")
    coordinated_fix_plan = Column(Text, default="")
    sub_items_json = Column(Text, default="")
    markdown_content = Column(Text, nullable=True)  # 完整 Markdown 報告文字，無須儲存為磁碟 .md 檔案
    raw_json = Column(Text, nullable=True)          # 原始錯誤清單與診斷上下文 JSON
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("service_name", "report_date", name="uq_svc_date"),
    )


# ============================================================================
# 初始化與自動遷移 (Auto-Migration)
# ============================================================================

def init_db():
    """建立所有資料表並執行無損遷移"""
    Base.metadata.create_all(bind=engine)
    session = SessionScoped()
    try:
        _auto_migrate_from_files(session)
    finally:
        session.close()


def _auto_migrate_from_files(session):
    """
    🛡️ 平滑自癒：若資料庫初建，自動讀取現存 projects.json 與 reports/**/issue_state.json，
    100% 完整無損將舊資料匯入資料庫。
    """
    # 1. 遷移 projects.json
    projects_file = os.path.join(ROOT_DIR, "projects.json")
    if os.path.exists(projects_file):
        try:
            with open(projects_file, "r", encoding="utf-8") as pf:
                projects_data = json.load(pf)
            for sname, conf in projects_data.items():
                existing = session.query(Project).filter_by(service_name=sname).first()
                if not existing:
                    proj = Project(
                        service_name=sname,
                        project_path=conf.get("project_path", ""),
                        project_id=conf.get("project_id"),
                        branch=conf.get("branch", "main"),
                        description=conf.get("description", ""),
                        log_field=conf.get("log_field", ""),
                        log_value=conf.get("log_value", ""),
                        notification_emails=conf.get("notification_emails", ""),
                        email_alert_enabled=conf.get("email_alert_enabled", True)
                    )
                    session.add(proj)
            session.commit()
            print("🛡️ [Database Auto-Migrate] 成功將 projects.json 匯入資料庫！")
        except Exception as e:
            session.rollback()
            print(f"⚠️ [Database Auto-Migrate] 遷移 projects.json 失敗: {e}")

    # 2. 遷移 reports/**/issue_state.json
    reports_dir = os.path.join(ROOT_DIR, "reports")
    if os.path.exists(reports_dir):
        try:
            for sname in os.listdir(reports_dir):
                s_dir = os.path.join(reports_dir, sname)
                state_file = os.path.join(s_dir, "issue_state.json")
                if os.path.isdir(s_dir) and os.path.exists(state_file):
                    with open(state_file, "r", encoding="utf-8") as sf:
                        s_data = json.load(sf)
                    for fp, it in s_data.get("issues", {}).items():
                        existing_issue = session.query(Issue).filter_by(fingerprint=fp).first()
                        if not existing_issue:
                            ca = it.get("cached_analysis")
                            ca_json = json.dumps(ca, ensure_ascii=False) if ca else None
                            new_issue = Issue(
                                fingerprint=fp,
                                service_name=sname,
                                error_type=it.get("error_type", "UnknownError"),
                                error_location=it.get("error_location", "unknown:0"),
                                first_seen_date=it.get("first_seen_date", ""),
                                last_seen_date=it.get("last_seen_date", ""),
                                consecutive_days=it.get("consecutive_days", 1),
                                today_count=it.get("today_count", 0),
                                yesterday_count=it.get("yesterday_count", 0),
                                total_occurrences=it.get("total_occurrences", 0),
                                status=it.get("status", "new"),
                                acknowledge_note=it.get("acknowledge_note"),
                                acknowledged_at=it.get("acknowledged_at"),
                                cached_analysis_json=ca_json
                            )
                            session.add(new_issue)

                        # 遷移 daily_history
                        for d, cnt in it.get("daily_history", {}).items():
                            hist = session.query(IssueDailyHistory).filter_by(fingerprint=fp, date=d).first()
                            if not hist:
                                session.add(IssueDailyHistory(fingerprint=fp, date=d, count=cnt))
            session.commit()
            print("🛡️ [Database Auto-Migrate] 成功將歷史異常紀錄與 7 天趨勢匯入資料庫！")
        except Exception as e:
            session.rollback()
            print(f"⚠️ [Database Auto-Migrate] 遷移 issue_state.json 失敗: {e}")


# ============================================================================
# 未來無痛直轉 MySQL 遷移工具 (One-Click Migration to MySQL)
# ============================================================================

def migrate_sqlite_to_mysql(target_mysql_url: str):
    """
    一鍵將本地 SQLite 資料庫全數注入目標 MySQL 資料庫：
    python -c "from core.database import migrate_sqlite_to_mysql; migrate_sqlite_to_mysql('mysql+pymysql://root:pass@host:3306/db')"
    """
    print(f"🚀 開始將 SQLite 資料庫無損遷移至目標 MySQL: {target_mysql_url} ...")
    target_engine = create_engine(target_mysql_url, pool_pre_ping=True)
    Base.metadata.create_all(bind=target_engine)
    
    TargetSession = sessionmaker(bind=target_engine)
    src_session = SessionScoped()
    tgt_session = TargetSession()

    try:
        # 1. 搬遷 Projects
        for p in src_session.query(Project).all():
            tgt_session.merge(Project(
                service_name=p.service_name,
                project_path=p.project_path,
                project_id=p.project_id,
                branch=p.branch,
                description=p.description,
                log_field=p.log_field,
                log_value=p.log_value,
                notification_emails=p.notification_emails,
                email_alert_enabled=p.email_alert_enabled
            ))
        
        # 2. 搬遷 Issues
        for i in src_session.query(Issue).all():
            tgt_session.merge(Issue(
                fingerprint=i.fingerprint,
                service_name=i.service_name,
                error_type=i.error_type,
                error_location=i.error_location,
                first_seen_date=i.first_seen_date,
                last_seen_date=i.last_seen_date,
                consecutive_days=i.consecutive_days,
                today_count=i.today_count,
                yesterday_count=i.yesterday_count,
                total_occurrences=i.total_occurrences,
                status=i.status,
                acknowledge_note=i.acknowledge_note,
                acknowledged_at=i.acknowledged_at,
                cached_analysis_json=i.cached_analysis_json
            ))
        
        # 3. 搬遷 Daily History
        for h in src_session.query(IssueDailyHistory).all():
            tgt_session.merge(IssueDailyHistory(
                fingerprint=h.fingerprint,
                date=h.date,
                count=h.count
            ))
            
        # 4. 搬遷 Diagnoses
        for d in src_session.query(Diagnosis).all():
            tgt_session.merge(Diagnosis(
                service_name=d.service_name,
                report_date=d.report_date,
                severity=d.severity,
                title=d.title,
                root_cause_summary=d.root_cause_summary,
                coordinated_fix_plan=d.coordinated_fix_plan,
                sub_items_json=d.sub_items_json
            ))
            
        tgt_session.commit()
        print("🎉 恭喜！SQLite 資料庫已 100% 成功無損搬遷至 MySQL！")
    except Exception as e:
        tgt_session.rollback()
        print(f"❌ 遷移失敗: {e}")
    finally:
        src_session.close()
        tgt_session.close()


def get_db_session():
    """取得資料庫 Session 上下文"""
    return SessionScoped()
