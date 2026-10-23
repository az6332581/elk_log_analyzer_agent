# -*- coding: utf-8 -*-
import pytest
from core.issue_tracker import normalize_error_signature, compute_fingerprint, IssueLifecycleTracker
from core.database import init_db, get_db_session, Project, Issue, IssueDailyHistory

def test_01_normalization_and_fingerprint():
    """1. 測試錯誤字串遮罩與指紋特徵一致性"""
    raw_1 = "Slow query found (time: 187.04ms) on connection 192.168.1.50"
    raw_2 = "Slow query found (time: 2500.8ms) on connection 10.0.0.12"
    
    norm_1 = normalize_error_signature(raw_1)
    norm_2 = normalize_error_signature(raw_2)
    
    assert "<duration>" in norm_1
    assert "<duration>" in norm_2
    assert "<ip>" in norm_1
    assert "<ip>" in norm_2
    
    fp_1 = compute_fingerprint("isocialwork", "DB::query", norm_1)
    fp_2 = compute_fingerprint("isocialwork", "DB::query", norm_2)
    assert fp_1 == fp_2, "相同根本原因但不同執行時間的錯誤，必須計算出完全一致之 12 碼指紋"
    assert len(fp_1) == 12

def test_02_database_orm_operations():
    """2. 測試資料庫 ORM 建表與基礎讀寫"""
    init_db()
    session = get_db_session()
    
    # 測試 Project 表
    test_svc = "_unit_test_service_"
    proj = session.query(Project).filter_by(service_name=test_svc).first()
    if not proj:
        proj = Project(service_name=test_svc, branch="feat/ci", description="測試微服務")
        session.add(proj)
        session.commit()
    
    loaded = session.query(Project).filter_by(service_name=test_svc).first()
    assert loaded is not None
    assert loaded.branch == "feat/ci"
    
    # 清理測試資料
    session.delete(loaded)
    session.commit()
    session.close()

def test_03_issue_lifecycle_state_machine(tmp_path):
    """3. 測試異常生命週期狀態機演變 (新問題 -> 連續頑疾 -> 標記已知 -> 恢復)"""
    tracker = IssueLifecycleTracker(base_dir=str(tmp_path))
    svc = "test-service"
    
    # 第 1 天：新發現問題
    errors_day1 = [{"error_type": "NullPointerException", "error_location": "User.java:42", "count": 5}]
    summary_day1 = tracker.update_lifecycle(svc, errors_day1, today_date_str="2026-10-20")
    
    assert summary_day1["new_count"] == 1
    assert summary_day1["chronic_count"] == 0
    assert summary_day1["max_consecutive_days"] == 1
    
    # 第 2 天：同一錯誤持續發生 -> 轉為連續頑疾
    errors_day2 = [{"error_type": "NullPointerException", "error_location": "User.java:42", "count": 8}]
    summary_day2 = tracker.update_lifecycle(svc, errors_day2, today_date_str="2026-10-21")
    
    assert summary_day2["new_count"] == 0
    assert summary_day2["chronic_count"] == 1
    assert summary_day2["max_consecutive_days"] == 2
    
    # 測試標記已知待排期 (Acknowledge)
    fp = errors_day2[0]["fingerprint"]
    ack_res = tracker.acknowledge_issue(svc, fp, note="已知技術債，排期下週修復")
    assert ack_res is True
    
    state = tracker.load_state(svc)
    assert state["issues"][fp]["status"] == "acknowledged"
    assert state["issues"][fp]["is_acknowledged"] is True
    
    # 測試恢復追蹤 (Unacknowledge)
    unack_res = tracker.unacknowledge_issue(svc, fp)
    assert unack_res is True
    state_after = tracker.load_state(svc)
    assert state_after["issues"][fp]["status"] == "ongoing"

def test_04_email_alert_decision_logic():
    """4. 測試 Email 智慧告警降噪決策閥門"""
    tracker = IssueLifecycleTracker()
    svc = "test-service"
    
    # 情境 A: 包含全新問題 -> 必須發信
    send_a, reason_a = tracker.should_send_email_alert(svc, {"new_count": 1, "is_escalated": False})
    assert send_a is True
    assert "全新異常" in reason_a
    
    # 情境 B: 全部為已知頑疾且無惡化 -> 自動靜音
    send_b, reason_b = tracker.should_send_email_alert(svc, {"new_count": 0, "chronic_count": 3, "is_escalated": False})
    assert send_b is False
    assert "靜音保護" in reason_b
    
    # 情境 C: 錯誤量飆升 3 倍以上 (惡化) -> 必須發信
    send_c, reason_c = tracker.should_send_email_alert(svc, {"new_count": 0, "is_escalated": True, "total_today_errors": 100, "total_yesterday_errors": 20})
    assert send_c is True
    assert "惡化" in reason_c

def test_05_web_dashboard_html_render():
    """5. 測試前端儀表板 HTML 生成冒煙測試 (確保模板語法無崩潰)"""
    from core.web_dashboard import render_dashboard_html, get_services_health
    
    data = get_services_health("reports")
    html = render_dashboard_html(data, gitlab_url="http://192.168.20.112:8929")
    
    assert html is not None
    assert "<!DOCTYPE html>" in html
    assert "微服務日誌分析平台" in html
    assert "儀表板" in html

