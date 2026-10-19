from __future__ import annotations

import os
import sys
import io
import json
import re
import argparse
import httpx
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from openai import OpenAI
from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse

from core.report_generator import (
    save_or_update_master_report,
    update_reports_index_dashboard,
    get_current_week_str,
    save_project_weekly_report,
    save_global_weekly_report,
    list_all_weekly_reports
)
from core.web_dashboard import (
    get_services_health,
    render_dashboard_html,
    render_report_view_html,
    render_log_explorer_html,
    render_weekly_dashboard_html
)
from core.repo_manager import (
    sync_repo,
    get_project_config,
    load_projects_config,
    add_or_update_project,
    delete_project,
    fetch_gitlab_branches
)
from pydantic import BaseModel
from skills import TOOLS_SCHEMA, execute_skill
from skills.code_tools import fetch_code_snippet
from core.issue_tracker import tracker as issue_tracker

# 強制設定標準輸出為 UTF-8 編碼，防止 Windows 下 cp950 崩潰
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# 載入環境變數
load_dotenv()

# 初始化 Elasticsearch 客戶端
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
ES_INDEX = os.getenv("ES_INDEX", "logs-*")
ES_USER = os.getenv("ES_USER")
ES_PASSWORD = os.getenv("ES_PASSWORD")

if ES_USER and ES_PASSWORD:
    es_client = Elasticsearch(
        [ES_HOST],
        basic_auth=(ES_USER, ES_PASSWORD)
    )
else:
    es_client = Elasticsearch([ES_HOST])

# 初始化 OpenAI 客戶端 (相容 NCHC 或原生 OpenAI)
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma-4-26B-A4B-it")

openai_client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_ENDPOINT
)

# 運行環境
APP_ENV = os.getenv("APP_ENV", "production")

# GitLab 自動開單設定
GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GITLAB_PROJECT_ID = os.getenv("GITLAB_PROJECT_ID")
GITLAB_TRIGGER_TOKEN = os.getenv("GITLAB_TRIGGER_TOKEN")

# ChatOps 伺服器與外網/內網存取設定 (預設 8300 避開已被佔用的 8200)
DEFAULT_SERVER_PORT = int(os.getenv("SERVER_PORT", "8300"))
CHATOPS_BASE_URL = os.getenv("CHATOPS_BASE_URL", f"http://localhost:{DEFAULT_SERVER_PORT}").rstrip("/")

# 告警推播設定 (SMTP 郵件與 Webhook)
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("ALERT_WEBHOOK_URL")
SCAN_HOURS = int(os.getenv("SCAN_HOURS", "24"))  # 巡檢掃描範圍 (預設 24 小時)
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))  # 預設每 1 小時自動巡檢一次 (60 分鐘)

def send_alert_notification(service_name: str, task_info: dict, report_url: str = None) -> bool:
    """
    發送異常告警通知 (支援 Email HTML 郵件與 Discord/Slack Webhook)
    支援專案級獨立收件人 (notification_emails)，若無則回退至全域 EMAIL_RECEIVER
    """
    title = task_info.get("title", f"[SRE-Daily] {service_name} 異常告警")
    severity = task_info.get("severity", "High")
    correlation = task_info.get("correlation_analysis", "系統檢測到異常日誌")
    fix_plan = task_info.get("coordinated_fix_plan", "")
    
    # 優先讀取該專案自訂的收件人，若未設定則使用全域 EMAIL_RECEIVER
    proj_conf = get_project_config(service_name) or {}
    target_emails = proj_conf.get("notification_emails") or EMAIL_RECEIVER
    
    email_sent = False
    # 1. 發送 Email 通知
    if SMTP_USER and SMTP_PASSWORD and target_emails:
        try:
            # 支援逗號或分號分隔多個收件人
            recipients = [e.strip() for e in re.split(r"[,;]", target_emails) if e.strip()]
            
            # 嚴重度徽章顏色
            badge_bg = "#ffe4e6" if severity in ("Critical", "High") else "#fef3c7"
            badge_color = "#e11d48" if severity in ("Critical", "High") else "#d97706"
            
            html_body = f"""
            <html>
                <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f8fafc; padding: 24px; color: #1e293b;">
                    <div style="max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; padding: 32px; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
                        <div style="border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; margin-bottom: 24px;">
                            <span style="background: {badge_bg}; color: {badge_color}; font-weight: 700; font-size: 11px; padding: 4px 12px; border-radius: 9999px; letter-spacing: 0.5px;">{severity.upper()} SEVERITY</span>
                            <h2 style="color: #0f172a; margin: 12px 0 6px 0; font-size: 20px;">🚨 SRE AI Sentinel 異常警報：{service_name}</h2>
                            <p style="color: #64748b; font-size: 13px; margin: 0;">檢測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)</p>
                        </div>
                        
                        <div style="margin-bottom: 20px;">
                            <h4 style="color: #334155; margin: 0 0 8px 0; font-size: 14px;">🌐 連鎖因果分析：</h4>
                            <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 14px 16px; border-radius: 10px; font-size: 13px; color: #1e293b; line-height: 1.6;">
                                {correlation}
                            </div>
                        </div>

                        <div style="margin-bottom: 24px;">
                            <h4 style="color: #334155; margin: 0 0 8px 0; font-size: 14px;">🛠️ 建議修復方案：</h4>
                            <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 14px 16px; border-radius: 10px; font-size: 13px; color: #1e293b; line-height: 1.6;">
                                {fix_plan[:350]}...
                            </div>
                        </div>

                        <div style="margin-top: 28px; text-align: center;">
                            <a href="{report_url or CHATOPS_BASE_URL}" target="_blank" style="display: inline-block; background: #4f46e5; color: #ffffff; text-decoration: none; padding: 12px 28px; border-radius: 10px; font-weight: 600; font-size: 14px; box-shadow: 0 2px 4px rgba(79,70,229,0.2);">
                                📄 前往 Web 查看完整診斷與自癒工單 →
                            </a>
                        </div>
                        
                        <div style="margin-top: 32px; padding-top: 16px; border-top: 1px solid #f1f5f9; text-align: center; color: #94a3b8; font-size: 11px;">
                            SRE AI Sentinel 自動巡檢與告警系統 ｜ 守護服務穩定運行
                        </div>
                    </div>
                </body>
            </html>
            """
            msg = MIMEText(html_body, 'html', 'utf-8')
            msg['Subject'] = Header(f"🚨 [SRE Sentinel] {service_name} 偵測到異常 ({severity})", 'utf-8')
            msg['From'] = Header(f"SRE AI Sentinel <{SMTP_USER}>", 'utf-8')
            msg['To'] = ", ".join(recipients)

            # 依據 Port 判斷使用 SSL (465) 或 STARTTLS (587/25)
            if SMTP_PORT == 465:
                server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=12)
            else:
                server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=12)
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
            server.quit()
            print(f"📧 成功發送異常告警郵件至 {recipients}")
            email_sent = True
        except Exception as e:
            print(f"⚠️ 發送告警郵件失敗: {e}")

    # 2. 發送 Webhook (Discord / Slack 格式)
    if DISCORD_WEBHOOK_URL:
        try:
            payload = {
                "embeds": [{
                    "title": f"🚨 [SRE Alert] {service_name} 發生嚴重異常！",
                    "description": f"**嚴重度**：`{severity}`\n**因果分析**：{correlation[:200]}...\n**修復建議**：{fix_plan[:200]}...",
                    "color": 15746887,  # 紅色
                    "url": report_url or CHATOPS_BASE_URL,
                    "footer": {"text": "SRE AI Sentinel 自動巡檢警報"}
                }]
            }
            httpx.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5.0)
            print(f"📡 成功推送 Webhook 告警至通訊軟體！")
        except Exception as e:
            print(f"⚠️ 推送 Webhook 告警失敗: {e}")

def send_weekly_email_notification(target_title: str, service_name: str, ai_analysis: dict, report_url: str = None) -> bool:
    """
    發送每週 SRE 營運綜整週報通知郵件
    - 若 service_name 存在，優先寄給該專案之 notification_emails
    - 若為全域週報，寄送至全域 EMAIL_RECEIVER
    """
    if not (SMTP_USER and SMTP_PASSWORD):
        return False
        
    proj_conf = get_project_config(service_name) if service_name else {}
    target_emails = proj_conf.get("notification_emails") if proj_conf else None
    if not target_emails:
        target_emails = EMAIL_RECEIVER
        
    if not target_emails:
        return False
        
    try:
        recipients = [e.strip() for e in re.split(r"[,;，；]", target_emails) if e.strip()]
        if not recipients:
            return False
            
        severity = ai_analysis.get("severity", "Normal")
        summary = ai_analysis.get("trend_summary") or ai_analysis.get("executive_summary") or "本週系統營運綜整完成。"
        
        badge_bg = "#ffe4e6" if severity in ("Critical", "High") else ("#fef3c7" if severity in ("Warning", "Medium") else "#d1fae5")
        badge_color = "#e11d48" if severity in ("Critical", "High") else ("#d97706" if severity in ("Warning", "Medium") else "#059669")
        
        url_btn = f'''
        <div style="margin-top: 24px; text-align: center;">
            <a href="{report_url}" style="background-color: #4f46e5; color: #ffffff; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 14px; display: inline-block;">
                👉 點此前往週報中心查看完整分析
            </a>
        </div>
        ''' if report_url else ""
        
        html_body = f"""
        <html>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f8fafc; padding: 24px; color: #1e293b;">
                <div style="max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; padding: 32px; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
                    <div style="border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; margin-bottom: 24px;">
                        <span style="background: {badge_bg}; color: {badge_color}; font-weight: 700; font-size: 11px; padding: 4px 12px; border-radius: 9999px; letter-spacing: 0.5px;">{severity.upper()}</span>
                        <h2 style="color: #0f172a; margin: 12px 0 6px 0; font-size: 20px;">📅 {target_title}</h2>
                        <p style="color: #64748b; font-size: 13px; margin: 0;">出刊時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)</p>
                    </div>
                    
                    <div style="margin-bottom: 20px;">
                        <h4 style="color: #334155; margin: 0 0 8px 0; font-size: 14px;">📊 本週營運與穩定度走勢摘要：</h4>
                        <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 16px; border-radius: 10px; font-size: 13px; color: #1e293b; line-height: 1.6;">
                            {summary}
                        </div>
                    </div>
                    {url_btn}
                    <div style="margin-top: 32px; border-top: 1px solid #e2e8f0; padding-top: 16px; text-align: center; color: #94a3b8; font-size: 12px;">
                        SRE AI Sentinel Observability Platform &bull; Weekly Report Service
                    </div>
                </div>
            </body>
        </html>
        """
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = Header(f"📅 [SRE 週報] {target_title}", "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = ", ".join(recipients)
        
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=12)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=12)
            server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, recipients, msg.as_string())
        server.quit()
        print(f"📧 成功發送週報郵件至 {recipients}")
        return True
    except Exception as e:
        print(f"⚠️ 發送週報郵件失敗: {e}")
        return False

# ============================================================================
# FastAPI 內嵌伺服器邏輯
# ============================================================================
app = FastAPI(title="AI-Debug ChatOps Proxy Server")

import asyncio

async def scheduled_scanner_loop():
    """背景定時巡檢任務：每 SCAN_INTERVAL_MINUTES 分鐘自動執行全域掃描與告警"""
    await asyncio.sleep(10)  # 伺服器啟動 10 秒後執行首次巡檢排程
    while True:
        try:
            print(f"\n⏰ [排程巡檢] 開始執行定時多專案健康檢查 (過去 {SCAN_HOURS} 小時, 間隔: {SCAN_INTERVAL_MINUTES} 分鐘)...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, main, SCAN_HOURS, None)
            print(f"⏰ [排程巡檢] 定時檢查完成，下次檢查將在 {SCAN_INTERVAL_MINUTES} 分鐘後。")

            # 檢查是否為週一且尚未生成本週週報，若是則自動執行每週營運週報批次生成與推播
            now = datetime.now()
            current_w = get_current_week_str(now)
            weekly_record_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", ".last_weekly_run")
            last_run_w = ""
            if os.path.exists(weekly_record_file):
                try:
                    with open(weekly_record_file, "r", encoding="utf-8") as wf:
                        last_run_w = wf.read().strip()
                except Exception:
                    pass
            if now.weekday() == 0 and last_run_w != current_w:
                print(f"\n📅 [週報排程] 檢測到新的一週 ({current_w}) 開始，自動啟動全域與各專案 SRE 週報生成...")
                projects_cfg = load_projects_config()
                for sname in projects_cfg.keys():
                    await loop.run_in_executor(None, generate_project_weekly_report, sname, current_w, None, True)
                await loop.run_in_executor(None, generate_global_weekly_report, current_w, None, True)
                try:
                    with open(weekly_record_file, "w", encoding="utf-8") as wf:
                        wf.write(current_w)
                except Exception:
                    pass
                print(f"✅ [週報排程] 第 {current_w} 週 SRE 週報已全數產出並發送通知！")
        except Exception as e:
            print(f"⚠️ [排程巡檢] 執行異常: {e}")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)

@app.on_event("startup")
async def on_app_startup():
    # 啟動非同步定時巡檢迴圈
    asyncio.create_task(scheduled_scanner_loop())

@app.get("/heal", response_class=HTMLResponse)
async def trigger_heal(issue_iid: int = Query(..., description="Issue IID to trigger self-healing")):
    if not GITLAB_TRIGGER_TOKEN:
        return """
        <html>
            <body style="font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #fff0f0;">
                <h2 style="color: #d9534f;">❌ 觸發失敗</h2>
                <p>本地環境變數中未配置 GITLAB_TRIGGER_TOKEN！</p>
            </body>
        </html>
        """
    
    # 拼裝 GitLab Pipeline Trigger 官方 POST URL
    trigger_url = f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/trigger/pipeline"
    data = {
        "token": GITLAB_TRIGGER_TOKEN,
        "ref": "feat/sre-ai-agent",
        "variables[TRIGGER_HEAL]": "true",
        "variables[ISSUE_IID]": str(issue_iid)
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(trigger_url, data=data, timeout=10.0)
            
        if resp.status_code == 201:
            pipeline_data = resp.json()
            pipeline_url = pipeline_data.get("web_url")
            return f"""
            <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #f4f9f4; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; border: 1px solid #d4edda; border-radius: 8px; padding: 30px; background-color: #fff; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
                        <h2 style="color: #28a745; margin-bottom: 20px;">🤖 AI 協同自癒 Pipeline 已成功啟動！</h2>
                        <p style="font-size: 16px;">訊號已順利發送至 GitLab Runner 容器，開始執行多檔案協同修補與全量測試驗證。</p>
                        <p style="font-size: 16px; margin: 20px 0;">
                            <strong>任務工單編號 (Issue IID)</strong>: <span style="color: #0056b3;">#{issue_iid}</span>
                        </p>
                        <a href="{pipeline_url}" target="_blank" style="display: inline-block; background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; margin-top: 10px;">
                            👉 前往 GitLab 查看 Pipeline 實況
                        </a>
                    </div>
                </body>
            </html>
            """
        else:
            return f"""
            <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #fff0f0;">
                    <div style="max-width: 600px; margin: 0 auto; border: 1px solid #f8d7da; border-radius: 8px; padding: 30px; background-color: #fff;">
                        <h2 style="color: #d9534f;">❌ 觸發 GitLab 失敗</h2>
                        <p>HTTP 狀態碼: {resp.status_code}</p>
                        <pre style="text-align: left; background: #eee; padding: 15px; border-radius: 5px; overflow-x: auto;">{resp.text}</pre>
                    </div>
                </body>
            </html>
            """
    except Exception as e:
        return f"""
        <html>
            <body style="font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #fff0f0;">
                <div style="max-width: 600px; margin: 0 auto; border: 1px solid #f8d7da; border-radius: 8px; padding: 30px; background-color: #fff;">
                    <h2 style="color: #d9534f;">❌ 系統例外錯誤</h2>
                    <p>{str(e)}</p>
                </div>
            </body>
        </html>
        """

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(env: str = Query(None, description="過濾指定環境 (如 production, development, local)")):
    reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    data = get_services_health(reports_base_dir, target_env=env)
    return render_dashboard_html(data, gitlab_url=GITLAB_URL or "http://192.168.20.112:8929", current_env=env or "")

@app.get("/report/{service_name}/{filename}", response_class=HTMLResponse)
async def view_report(service_name: str, filename: str):
    reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    file_path = os.path.join(reports_base_dir, service_name, filename)
    if not os.path.exists(file_path):
        return HTMLResponse(content="<div style='font-family: sans-serif; padding: 40px; text-align: center;'><h2>❌ 找不到該診斷報告檔案</h2><p><a href='/'>返回儀表板</a></p></div>", status_code=404)
    with open(file_path, "r", encoding="utf-8") as f:
        md_content = f.read()
    return render_report_view_html(service_name, filename, md_content)

@app.get("/logs", response_class=HTMLResponse)
async def log_explorer_page(
    service: str = Query(None, description="預設微服務專案名稱"),
    environment: str = Query(None, description="預設運行環境"),
    env: str = Query(None, description="預設運行環境別名"),
    time_range: str = Query(None, description="預設時間範圍 (例如 1h, 6h, 24h, 48h, 7d)")
):
    """
    即時日誌探測器 (Log Explorer) 頁面
    提供即時自訂時間範圍、專案與等級篩選
    """
    target_env = environment or env or ""
    projects_cfg = load_projects_config()
    return render_log_explorer_html(
        projects_cfg,
        default_service=service or "",
        default_env=target_env,
        default_time_range=time_range or ""
    )

@app.get("/api/logs/environments")
async def api_logs_environments(service_name: str = Query(None, description="可選微服務名稱")):
    """
    即時向 Elasticsearch 聚合查詢當前實際存在的所有運行環境標籤 (如 production, development, local)
    """
    envs = get_available_environments(service_name=service_name)
    return {"status": "ok", "environments": envs}

@app.get("/api/logs/query")
async def api_query_logs(
    service_name: str = Query(None, description="微服務名稱"),
    environment: str = Query(None, description="運行環境 (如 production, development, local 等)"),
    hours: int = Query(None, description="過去 X 小時"),
    start_time: str = Query(None, description="起始時間 (ISO/UTC 或字串)"),
    end_time: str = Query(None, description="結束時間 (ISO/UTC 或字串)"),
    level: str = Query(None, description="日誌等級 (ERROR, WARN, INFO 等)"),
    keyword: str = Query(None, description="關鍵字全文搜尋"),
    limit: int = Query(100, description="筆數限制，上限 500")
):
    """
    提供前端 Log Explorer 查詢即時 Elasticsearch 日誌
    """
    import time
    t0 = time.time()
    try:
        logs, total = search_logs_flexible(
            service_name=service_name,
            environment=environment,
            hours=hours,
            start_time=start_time,
            end_time=end_time,
            level=level,
            keyword=keyword,
            limit=limit
        )
        elapsed = int((time.time() - t0) * 1000)
        return {
            "status": "ok",
            "logs": logs,
            "total": total,
            "count": len(logs),
            "elapsed_ms": elapsed,
            "index_name": ES_INDEX
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "logs": [],
            "total": 0
        }

@app.get("/api/logs/histogram")
async def api_logs_histogram(
    service_name: str = Query(None, description="微服務名稱"),
    environment: str = Query(None, description="運行環境 (如 production, development 等)"),
    hours: int = Query(24, description="統計過去 X 小時 (預設 24)"),
    start_time: str = Query(None, description="起始時間"),
    end_time: str = Query(None, description="結束時間")
):
    """
    提供 Log Explorer 繪製日誌時間分佈趨勢長條圖 (Histogram)
    """
    try:
        data = search_logs_histogram(
            service_name=service_name,
            environment=environment,
            hours=hours,
            start_time=start_time,
            end_time=end_time
        )
        return {"status": "ok", "buckets": data}
    except Exception as e:
        return {"status": "error", "message": str(e), "buckets": []}

@app.get("/api/gitlab/branches")
async def api_get_gitlab_branches(project_path: str = Query(..., description="GitLab 專案路徑或 Project ID")):
    """
    即時向 GitLab API 查詢該專案的遠端真實分支列表，供前端防呆下拉選單使用
    """
    try:
        branches = fetch_gitlab_branches(project_path.strip())
        return {
            "status": "ok",
            "branches": branches,
            "count": len(branches)
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "branches": []}


@app.get("/api/status")
async def api_status():
    reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    return get_services_health(reports_base_dir)

is_scanning_active = False

@app.post("/api/scan")
async def api_trigger_scan(hours: int = Query(None, description="自訂掃描時數")):
    global is_scanning_active
    if is_scanning_active:
        return {"status": "ok", "message": "目前已有掃描與 AI 診斷正在進行中，請稍候片刻完成！"}
    
    import asyncio
    scan_h = hours if hours is not None else SCAN_HOURS
    try:
        is_scanning_active = True
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, main, scan_h, None)
        return {"status": "ok", "message": f"日誌掃描與微服務診斷完成 (掃描過去 {scan_h} 小時)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        is_scanning_active = False

@app.post("/api/test/email")
async def api_test_email(service_name: str = Query(None, description="可選特定微服務名稱")):
    """發送一封測試告警郵件，驗證 SMTP 與郵件樣式是否正常連通 (支援測試特定專案的獨立收件人)"""
    sname = service_name or "demo-test-service"
    proj_conf = get_project_config(sname) or {}
    target = proj_conf.get("notification_emails") or EMAIL_RECEIVER or "未設定"
    
    test_task = {
        "title": f"[SRE-Test] 系統告警測試信 ({sname})",
        "severity": "High",
        "correlation_analysis": f"這是一封來自 SRE AI Sentinel 針對專案 [{sname}] 的測試告警信。當此專案發生異常時，系統會自動發送因果分析與修復計畫至專屬負責人信箱。",
        "coordinated_fix_plan": "1. 驗證 SMTP 伺服器通訊正常。\n2. 檢查此專案指定的負責人信箱是否已成功收到通知。\n3. 無需採取任何維運動作。"
    }
    success = send_alert_notification(sname, test_task, report_url=CHATOPS_BASE_URL)
    if success:
        return {"status": "ok", "message": f"測試郵件已成功寄出至專案收件人: {target}"}
    else:
        return {"status": "error", "message": f"郵件發送失敗，請確認 SMTP 設定 (發件人: {SMTP_USER}, 目標: {target})"}

@app.get("/health")
async def health_check():
    """容器健康檢查端點 (Docker HEALTHCHECK)"""
    return {"status": "ok"}

class ProjectCreateRequest(BaseModel):
    service_name: str
    description: str = ""
    project_path: str = ""
    project_id: int | None = None
    branch: str = "main"
    log_field: str = ""
    log_value: str = ""
    notification_emails: str = ""

@app.post("/api/projects")
async def api_add_project(req: ProjectCreateRequest, background_tasks: BackgroundTasks):
    sname = req.service_name.strip()
    if not sname:
        return {"status": "error", "message": "專案名稱 (service_name) 不能為空"}
    conf = {
        "project_path": req.project_path.strip(),
        "project_id": req.project_id,
        "branch": req.branch.strip() or "main",
        "description": req.description.strip() or "微服務應用程式",
        "log_field": req.log_field.strip(),
        "log_value": req.log_value.strip(),
        "notification_emails": req.notification_emails.strip()
    }
    success = add_or_update_project(sname, conf)
    if success:
        # 在背景自動執行 git clone / fetch 同步至 repos/ 資料夾，不卡住前端請求
        background_tasks.add_task(sync_repo, sname)
        return {"status": "ok", "message": f"專案 [{sname}] 成功儲存，已在背景自動同步最新程式碼"}
    return {"status": "error", "message": "寫入 projects.json 失敗"}

@app.delete("/api/projects/{service_name}")
async def api_delete_project(service_name: str):
    success = delete_project(service_name)
    if success:
        return {"status": "ok", "message": f"專案 [{service_name}] 已自監控清單移除"}
    return {"status": "error", "message": f"找不到專案 [{service_name}] 或移除失敗"}


@app.get("/weekly", response_class=HTMLResponse)
async def weekly_dashboard_page(
    service: str = Query(None, description="微服務名稱 (留空為全域週報)"),
    week: str = Query(None, description="指定週次 (例如 2026_W42)")
):
    """
    SRE AI Sentinel 每週總分析與營運週報中心頁面
    支援切換全域總控週報與各微服務專案專屬週報
    """
    reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    weekly_idx = list_all_weekly_reports(reports_base_dir)
    projects_cfg = load_projects_config()
    
    target_week = week or (weekly_idx["weeks"][0] if weekly_idx.get("weeks") else get_current_week_str())
    
    # 預設優先進入第一個微服務專案獨立週報，避免所有專案混在一起的大雜燴總表
    project_keys = list(projects_cfg.keys())
    if not service and project_keys:
        target_service = project_keys[0]
    else:
        target_service = service or ""
    
    # 讀取指定週報 Markdown
    md_content = ""
    if target_service:
        fpath = os.path.join(reports_base_dir, target_service, f"weekly_{target_week}.md")
    else:
        fpath = os.path.join(reports_base_dir, "weekly", f"weekly_{target_week}.md")
        
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                md_content = f.read()
        except Exception as e:
            print(f"⚠️ 讀取週報 Markdown 失敗: {e}")
            
    return render_weekly_dashboard_html(
        weekly_index=weekly_idx,
        current_service=target_service,
        current_week=target_week,
        md_content=md_content,
        projects_cfg=projects_cfg,
        default_email=EMAIL_RECEIVER or ""
    )


@app.post("/api/weekly/generate")
async def api_generate_weekly(
    service_name: str = Query(None, description="指定專案名稱，留空則全域產生"),
    week: str = Query(None, description="指定週次 (如 2026_W42)")
):
    """
    手動觸發週報生成：
    - 若指定 service_name：只生成該微服務專案的專屬週報並寄信
    - 若未指定 service_name：批次生成全域總控週報與所有專案週報
    """
    reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    w_str = week or get_current_week_str()
    loop = asyncio.get_event_loop()
    try:
        if service_name and service_name.strip():
            sname = service_name.strip()
            await loop.run_in_executor(None, generate_project_weekly_report, sname, w_str, reports_base_dir, True)
            return {"status": "ok", "message": f"專案 [{sname}] 的第 {w_str} 週報已成功生成！"}
        else:
            projects_cfg = load_projects_config()
            for sname in projects_cfg.keys():
                await loop.run_in_executor(None, generate_project_weekly_report, sname, w_str, reports_base_dir, True)
            await loop.run_in_executor(None, generate_global_weekly_report, w_str, reports_base_dir, True)
            return {"status": "ok", "message": f"全域總控與所有微服務的第 {w_str} 週報已全數批次生成完成！"}
    except Exception as e:
        return {"status": "error", "message": f"週報生成失敗: {e}"}


def fetch_recent_logs(hours: int = 24, level: str = "ERROR", index_name: str = None) -> list[dict]:
    """
    從 Elasticsearch 撈取最近 X 小時內的特定 Level 日誌。
    """
    target_index = index_name or ES_INDEX
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 正在從 Elasticsearch ({target_index}) 撈取過去 {hours} 小時的 {level} 日誌...")
    
    query = {
        "size": 500,
        "query": {
            "bool": {
                "must": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": f"now-{hours}h",
                                "lte": "now"
                            }
                        }
                    },
                    {
                        "term": {
                            "level.keyword": level
                        }
                    }
                ]
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}]
    }
    
    try:
        response = es_client.search(index=target_index, body=query)
        hits = response['hits']['hits']
        print(f"✅ 成功撈取到 {len(hits)} 筆 {level} 日誌。")
        return [hit['_source'] for hit in hits]
    except Exception as e:
        print(f"⚠️ 查詢 Elasticsearch 失敗: {e}")
        return []

def get_available_environments(service_name: str = None, index_name: str = None) -> list[str]:
    """
    動態向 Elasticsearch 聚合查詢當前實際存在的所有運行環境標籤 (相容 environment, service.environment, env)
    """
    target_index = index_name or ES_INDEX
    must = []
    if service_name and service_name.strip():
        sname = service_name.strip()
        projects_cfg = load_projects_config()
        conf = projects_cfg.get(sname, {})
        custom_field = str(conf.get("log_field") or "").strip()
        custom_val = str(conf.get("log_value") or "").strip() or sname

        if custom_field:
            c_fields = [custom_field]
            if "." in custom_field:
                c_fields.append(custom_field.replace(".", "_"))
            elif "_" in custom_field:
                c_fields.append(custom_field.replace("_", "."))
            sq = []
            for cf in c_fields:
                kw_field = f"{cf}.keyword" if not cf.endswith(".keyword") else cf
                sq.extend([
                    {"term": {kw_field: custom_val}},
                    {"term": {cf: custom_val}}
                ])
            must.append({"bool": {"should": sq, "minimum_should_match": 1}})
        else:
            must.append({
                "bool": {
                    "should": [
                        {"term": {"service.name.keyword": sname}},
                        {"term": {"service_name.keyword": sname}},
                        {"term": {"service.name": sname}},
                        {"term": {"service_name": sname}}
                    ],
                    "minimum_should_match": 1
                }
            })

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    agg_query = {
        "size": 0,
        "query": query,
        "aggs": {
            "top_env": {"terms": {"field": "environment.keyword", "size": 30}},
            "svc_env": {"terms": {"field": "service.environment.keyword", "size": 30}},
            "raw_env": {"terms": {"field": "env.keyword", "size": 30}}
        }
    }
    try:
        resp = es_client.search(index=target_index, body=agg_query)
        envs = set()
        for agg_k in ["top_env", "svc_env", "raw_env"]:
            for b in resp.get("aggregations", {}).get(agg_k, {}).get("buckets", []):
                k = b.get("key")
                if k and str(k).strip():
                    envs.add(str(k).strip())
        return sorted(list(envs))
    except Exception as e:
        print(f"⚠️ 聚合環境列表失敗: {e}")
        return ["production", "development", "local"]

def search_logs_flexible(
    service_name: str = None,
    environment: str = None,
    hours: int = None,
    start_time: str = None,
    end_time: str = None,
    level: str = None,
    keyword: str = None,
    limit: int = 100,
    index_name: str = None
) -> tuple[list[dict], int]:
    """
    【彈性多維度日誌檢索器】
    支援：
    1. 指定特定微服務 (自動套用 projects.json 的 log_field/log_value，或智慧 fallback 多欄位 should 查詢)
    2. 運行環境過濾 (production, development, local 等，相容 environment, service.environment, env)
    3. 時間範圍 (相對時間 hours 如 now-1h，或自訂 start_time/end_time ISO 格式)
    4. 日誌等級 (ERROR, WARN, INFO, DEBUG, 或全選)
    5. 關鍵字全文搜尋 (message, error_message, error_stack)
    6. 自訂回傳上限 limit
    """
    target_index = index_name or ES_INDEX
    must_conditions = []

    # 1. 時間範圍過濾
    if start_time or end_time:
        range_clause = {}
        if start_time:
            range_clause["gte"] = start_time
        if end_time:
            range_clause["lte"] = end_time
        must_conditions.append({"range": {"@timestamp": range_clause}})
    elif hours:
        must_conditions.append({
            "range": {
                "@timestamp": {
                    "gte": f"now-{hours}h",
                    "lte": "now"
                }
            }
        })

    # 2. 日誌等級過濾
    if level and level.strip():
        lvl = level.strip().upper()
        must_conditions.append({
            "bool": {
                "should": [
                    {"term": {"level.keyword": lvl}},
                    {"term": {"log.level": lvl.lower()}},
                    {"term": {"log.level.keyword": lvl}},
                    {"match": {"level": lvl}}
                ],
                "minimum_should_match": 1
            }
        })

    # 2.5 運行環境過濾 (相容 environment, service.environment, env 及大小寫)
    if environment and environment.strip():
        env_val = environment.strip()
        must_conditions.append({
            "bool": {
                "should": [
                    {"term": {"environment.keyword": env_val}},
                    {"term": {"service.environment.keyword": env_val}},
                    {"term": {"env.keyword": env_val}},
                    {"term": {"environment": env_val}},
                    {"term": {"service.environment": env_val}},
                    {"term": {"env": env_val}},
                    {"term": {"environment": env_val.lower()}},
                    {"term": {"service.environment": env_val.lower()}},
                    {"term": {"env": env_val.lower()}}
                ],
                "minimum_should_match": 1
            }
        })

    # 3. 特定專案過濾 (結合 projects.json 自訂規則 ＋ 智能相容解析)
    if service_name and service_name.strip():
        sname = service_name.strip()
        projects_cfg = load_projects_config()
        conf = projects_cfg.get(sname, {})
        custom_field = str(conf.get("log_field") or "").strip()
        custom_val = str(conf.get("log_value") or "").strip() or sname

        if custom_field:
            # 使用者在該專案自訂的精確欄位比對 (自動相容 service.name <-> service_name)
            c_fields = [custom_field]
            if "." in custom_field:
                c_fields.append(custom_field.replace(".", "_"))
            elif "_" in custom_field:
                c_fields.append(custom_field.replace("_", "."))

            should_queries = []
            for cf in c_fields:
                kw_field = f"{cf}.keyword" if not cf.endswith(".keyword") else cf
                should_queries.extend([
                    {"term": {kw_field: custom_val}},
                    {"term": {cf: custom_val}},
                    {"match_phrase": {cf: custom_val}}
                ])
            must_conditions.append({
                "bool": {
                    "should": should_queries,
                    "minimum_should_match": 1
                }
            })
        else:
            # 萬用微服務欄位相容比對
            must_conditions.append({
                "bool": {
                    "should": [
                        {"term": {"service.name.keyword": sname}},
                        {"term": {"service_name.keyword": sname}},
                        {"term": {"service.name": sname}},
                        {"term": {"service_name": sname}},
                        {"term": {"service.keyword": sname}},
                        {"term": {"app_name.keyword": sname}},
                        {"match_phrase": {"service_name": sname}},
                        {"match_phrase": {"service.name": sname}}
                    ],
                    "minimum_should_match": 1
                }
            })

    # 4. 關鍵字搜尋 (模糊匹配 message / 呼叫堆疊)
    if keyword and keyword.strip():
        kw = keyword.strip()
        must_conditions.append({
            "bool": {
                "should": [
                    {"wildcard": {"message": f"*{kw}*"}},
                    {"wildcard": {"error_message": f"*{kw}*"}},
                    {"match_phrase": {"message": kw}},
                    {"match_phrase": {"error_stack": kw}}
                ],
                "minimum_should_match": 1
            }
        })

    bool_query = {"must": must_conditions} if must_conditions else {"match_all": {}}
    search_limit = max(1, min(limit, 500))

    query_body = {
        "size": search_limit,
        "query": {"bool": bool_query} if "must" in bool_query else bool_query,
        "sort": [{"@timestamp": {"order": "desc"}}]
    }

    try:
        response = es_client.search(index=target_index, body=query_body)
        hits = response.get('hits', {}).get('hits', [])
        total_hits = response.get('hits', {}).get('total', {})
        total_count = total_hits.get('value', len(hits)) if isinstance(total_hits, dict) else len(hits)
        return [hit['_source'] for hit in hits], total_count
    except Exception as e:
        print(f"⚠️ search_logs_flexible 查詢失敗: {e}")
        return [], 0

def search_logs_histogram(
    service_name: str = None,
    environment: str = None,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    index_name: str = None
) -> list[dict]:
    """
    透過 Elasticsearch date_histogram 聚合，產生以小時為單位的日誌數量統計長條圖資料
    """
    target_index = index_name or ES_INDEX
    must_conditions = []

    # 1. 時間範圍
    if start_time or end_time:
        range_clause = {}
        if start_time:
            range_clause["gte"] = start_time
        if end_time:
            range_clause["lte"] = end_time
        must_conditions.append({"range": {"@timestamp": range_clause}})
    elif hours:
        must_conditions.append({
            "range": {
                "@timestamp": {
                    "gte": f"now-{hours}h",
                    "lte": "now"
                }
            }
        })

    # 1.5 運行環境過濾 (相容 environment, service.environment, env 及大小寫)
    if environment and environment.strip():
        env_val = environment.strip()
        must_conditions.append({
            "bool": {
                "should": [
                    {"term": {"environment.keyword": env_val}},
                    {"term": {"service.environment.keyword": env_val}},
                    {"term": {"env.keyword": env_val}},
                    {"term": {"environment": env_val}},
                    {"term": {"service.environment": env_val}},
                    {"term": {"env": env_val}},
                    {"term": {"environment": env_val.lower()}},
                    {"term": {"service.environment": env_val.lower()}},
                    {"term": {"env": env_val.lower()}}
                ],
                "minimum_should_match": 1
            }
        })

    # 2. 專案比對
    if service_name and service_name.strip():
        sname = service_name.strip()
        projects_cfg = load_projects_config()
        conf = projects_cfg.get(sname, {})
        custom_field = str(conf.get("log_field") or "").strip()
        custom_val = str(conf.get("log_value") or "").strip() or sname

        if custom_field:
            c_fields = [custom_field]
            if "." in custom_field:
                c_fields.append(custom_field.replace(".", "_"))
            elif "_" in custom_field:
                c_fields.append(custom_field.replace("_", "."))

            should_queries = []
            for cf in c_fields:
                kw_field = f"{cf}.keyword" if not cf.endswith(".keyword") else cf
                should_queries.extend([
                    {"term": {kw_field: custom_val}},
                    {"term": {cf: custom_val}}
                ])
            must_conditions.append({
                "bool": {
                    "should": should_queries,
                    "minimum_should_match": 1
                }
            })
        else:
            must_conditions.append({
                "bool": {
                    "should": [
                        {"term": {"service.name.keyword": sname}},
                        {"term": {"service_name.keyword": sname}},
                        {"term": {"service.name": sname}},
                        {"term": {"service_name": sname}}
                    ],
                    "minimum_should_match": 1
                }
            })

    bool_query = {"must": must_conditions} if must_conditions else {"match_all": {}}

    # 智能計算長條圖時間顆粒度 (Interval)，控制柱數約在 24~40 根之間，完美貼合卡片寬度
    total_hours = hours or 24
    if start_time and end_time:
        try:
            st_clean = start_time.replace("Z", "+00:00")
            et_clean = end_time.replace("Z", "+00:00")
            dt_s = datetime.fromisoformat(st_clean)
            dt_e = datetime.fromisoformat(et_clean)
            total_hours = max(1, int((dt_e - dt_s).total_seconds() / 3600))
        except Exception:
            total_hours = 24

    if total_hours <= 12:
        interval = "15m"
    elif total_hours <= 48:
        interval = "1h"
    elif total_hours <= 168: # 7 天內
        interval = "3h"
    elif total_hours <= 720: # 30 天內
        interval = "1d"
    else:
        interval = "7d"
    
    # 建立時間軸上下界 (確保即使 0 筆日誌也能展開完整時間軸)
    if start_time or end_time:
        ext_min = start_time or f"now-{hours or 24}h"
        ext_max = end_time or "now"
    else:
        ext_min = f"now-{hours or 24}h"
        ext_max = "now"

    agg_query = {
        "size": 0,
        "query": {"bool": bool_query} if "must" in bool_query else bool_query,
        "aggs": {
            "over_time": {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": interval,
                    "min_doc_count": 0,
                    "extended_bounds": {
                        "min": ext_min,
                        "max": ext_max
                    }
                },
                "aggs": {
                    "by_level": {
                        "filters": {
                            "filters": {
                                "error": {
                                    "bool": {
                                        "should": [
                                            {"terms": {"level.keyword": ["ERROR", "error", "CRITICAL", "critical", "FATAL", "fatal", "ERR", "err", "EMERGENCY", "emergency"]}},
                                            {"terms": {"log.level.keyword": ["ERROR", "error", "CRITICAL", "critical", "FATAL", "fatal", "ERR", "err", "EMERGENCY", "emergency"]}},
                                            {"terms": {"level": ["ERROR", "error", "CRITICAL", "critical", "FATAL", "fatal", "ERR", "err", "EMERGENCY", "emergency"]}},
                                            {"terms": {"log.level": ["ERROR", "error", "CRITICAL", "critical", "FATAL", "fatal", "ERR", "err", "EMERGENCY", "emergency"]}}
                                        ]
                                    }
                                },
                                "warn": {
                                    "bool": {
                                        "should": [
                                            {"terms": {"level.keyword": ["WARN", "warn", "WARNING", "warning"]}},
                                            {"terms": {"log.level.keyword": ["WARN", "warn", "WARNING", "warning"]}},
                                            {"terms": {"level": ["WARN", "warn", "WARNING", "warning"]}},
                                            {"terms": {"log.level": ["WARN", "warn", "WARNING", "warning"]}}
                                        ]
                                    }
                                },
                                "info": {
                                    "bool": {
                                        "should": [
                                            {"terms": {"level.keyword": ["INFO", "info", "DEBUG", "debug", "NOTICE", "notice", "TRACE", "trace"]}},
                                            {"terms": {"log.level.keyword": ["INFO", "info", "DEBUG", "debug", "NOTICE", "notice", "TRACE", "trace"]}},
                                            {"terms": {"level": ["INFO", "info", "DEBUG", "debug", "NOTICE", "notice", "TRACE", "trace"]}},
                                            {"terms": {"log.level": ["INFO", "info", "DEBUG", "debug", "NOTICE", "notice", "TRACE", "trace"]}}
                                        ]
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    try:
        resp = es_client.search(index=target_index, body=agg_query)
        buckets = resp.get("aggregations", {}).get("over_time", {}).get("buckets", [])
        result = []
        for b in buckets:
            key_as_string = b.get("key_as_string", "")
            total_doc = b.get("doc_count", 0)
            lvl_buckets = b.get("by_level", {}).get("buckets", {})
            err_cnt = lvl_buckets.get("error", {}).get("doc_count", 0)
            warn_cnt = lvl_buckets.get("warn", {}).get("doc_count", 0)
            info_cnt = lvl_buckets.get("info", {}).get("doc_count", 0)

            # 若有日誌但未明確歸類（例如無 level 欄位或特殊等級），剩餘數量全數補入 info，確保柱狀圖不留白
            classified = err_cnt + warn_cnt + info_cnt
            if total_doc > classified:
                info_cnt += (total_doc - classified)

            result.append({
                "time": key_as_string,
                "total": total_doc,
                "error": err_cnt,
                "warn": warn_cnt,
                "info": info_cnt
            })
        return result
    except Exception as e:
        print(f"⚠️ search_logs_histogram 聚合失敗: {e}")
        return []

def extract_service_name(log: dict) -> str:
    """
    智能正規化擷取微服務名稱，完全相容：
    1. log["service_name"] (例如 ["socialwork2-rag-backend"])
    2. log["service.name"] 或 log["service.name.keyword"] (例如 ["nchc-safty-alert"])
    3. log["service"]["name"] (Elastic ECS 巢狀 JSON 結構)
    4. log["app_name"] / log["application"]
    5. 自動解開陣列包裝 (list/tuple -> str)
    """
    raw = None
    if "service_name" in log:
        raw = log["service_name"]
    elif "service.name" in log:
        raw = log["service.name"]
    elif "service.name.keyword" in log:
        raw = log["service.name.keyword"]
    elif "service" in log and isinstance(log["service"], dict):
        raw = log["service"].get("name")
    elif "app_name" in log:
        raw = log["app_name"]
    elif "application" in log:
        raw = log["application"]

    if isinstance(raw, (list, tuple)) and len(raw) > 0:
        raw = raw[0]

    if raw and isinstance(raw, str):
        clean = raw.strip()
        if clean:
            return clean

    return "global"

def get_log_field_value(log: dict, field_path: str):
    """支援直接欄位鍵值以及點號階層語法 (如 service.name 或 kubernetes.pod_name)"""
    if not field_path or not isinstance(log, dict):
        return None
    candidates = [field_path]
    if "." in field_path:
        candidates.append(field_path.replace(".", "_"))
    elif "_" in field_path:
        candidates.append(field_path.replace("_", "."))

    for fp in candidates:
        if fp in log:
            return log[fp]
        if f"{fp}.keyword" in log:
            return log[f"{fp}.keyword"]
        parts = fp.split(".")
        curr = log
        found = True
        for p in parts:
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            else:
                found = False
                break
        if found:
            return curr
    return None

def match_log_to_service(log: dict, projects_cfg: dict) -> str:
    """
    雙重智能匹配：
    1. 優先比對各專案在網頁上自訂的日誌欄位 (log_field 與 log_value)
    2. 若未自訂或無命中，則自動回退至多欄位相容解析 extract_service_name(log)
    """
    if projects_cfg:
        for sname, conf in projects_cfg.items():
            custom_field = conf.get("log_field")
            if custom_field:
                expected_val = conf.get("log_value") or sname
                val = get_log_field_value(log, custom_field)
                if isinstance(val, (list, tuple)) and len(val) > 0:
                    val = val[0]
                if val and str(val).strip() == str(expected_val).strip():
                    return sname

    return extract_service_name(log)

def aggregate_errors(logs: list[dict]) -> list[dict]:
    """
    對 Error Log 進行聚類去重，合併相同 (error_location, error_type)。
    """
    aggregated = {}
    for log in logs:
        location = str(log.get("error_location") or "unknown:0").strip()
        err_type = str(log.get("error_type") or "unknown_type").strip()
        key = (location, err_type)
        
        err_msg = str(log.get("error_message") or "")
        err_stack = str(log.get("error_stack") or "")[:800]
        
        if key not in aggregated:
            aggregated[key] = {
                "error_location": location,
                "error_type": err_type,
                "error_message": err_msg,
                "role": str(log.get("role") or "N/A"),
                "count": 1,
                "sample_input": log.get("inputs", {}) if isinstance(log.get("inputs"), dict) else {},
                "error_stack_sample": err_stack
            }
        else:
            aggregated[key]["count"] += 1
            
    return list(aggregated.values())

def analyze_all_errors_holistically(
    aggregated_errors: list[dict],
    service_name: str = "global",
    lifecycle_summary: dict = None
) -> dict:
    """
    【全域批次分析 ＋ SRE 原生代碼技能 ＋ 智慧 Token 快取】
    依據微服務專案，自動調用技能切取出錯行前後 20 行真實代碼，
    若全部為已知連續頑疾則直接沿用歷史診斷快取，省去重覆 LLM 消耗；
    若有新問題或無快取則打包送入 AI 大腦進行因果關聯分析。
    """
    # 檢查是否可命中 AI 診斷快取 (無任何新問題、且所有項目均有前次分析快取)
    if (
        lifecycle_summary and 
        lifecycle_summary.get("new_count", 0) == 0 and 
        len(aggregated_errors) > 0 and 
        all(e.get("cached_analysis") for e in aggregated_errors)
    ):
        first_cache = aggregated_errors[0]["cached_analysis"]
        consecutive_d = lifecycle_summary.get("max_consecutive_days", 2)
        print(f"⚡ [AI 快取命中] 專案 [{service_name}] 的 {len(aggregated_errors)} 個問題均為已知連續頑疾 (已持續 {consecutive_d} 天)，直接沿用歷史修復建議 (節省 LLM Token)！")
        return {
            "title": f"[SRE-Daily] {service_name} 異常全景診斷與協同修復工單 ({datetime.now().strftime('%Y-%m-%d')})",
            "severity": first_cache.get("severity", "High"),
            "correlation_analysis": f"【⏳ 連續頑疾持續追蹤中（已連續 {consecutive_d} 天）】\n" + (first_cache.get("root_cause_summary") or first_cache.get("correlation_analysis") or "同前次分析結論，根本原因為外部服務連線或資源瓶頸。"),
            "coordinated_fix_plan": first_cache.get("coordinated_fix_plan", "依既有跨檔案修復計畫持續推進排期。"),
            "sub_items": [f"持續追蹤連續第 {consecutive_d} 天未修復問題，維持監控降級機制"]
        }

    print(f"🧠 正在請求 SRE AI 大腦 ({LLM_MODEL}) 對專案 [{service_name}] 進行全域關聯與代碼級協同分析...")
    
    errors_summary_list = []
    # 依發生次數由高到低排序，優先取 Top 15 關鍵重大錯誤進行深度切片與 LLM 分析，避免 Prompt 爆炸與過長等待
    top_errors = sorted(aggregated_errors, key=lambda x: x.get("count", 1), reverse=True)[:15]
    for idx, err in enumerate(top_errors, start=1):
        loc = str(err.get("error_location") or "unknown:0").strip()
        
        # 智慧解析檔案路徑與行號
        file_path = ""
        line_num = 1
        if ":" in loc:
            parts = loc.split(":")
            file_path = parts[0].strip()
            try:
                line_num = int(parts[1])
            except ValueError:
                line_num = 1
                
        # 調用技能切取出錯行前後 20 行原始碼（若遇第三方套件則自動回溯業務代碼）
        code_snippet = ""
        stack_sample = err.get("error_stack_sample") or ""
        if file_path:
            code_snippet = fetch_code_snippet(service_name, file_path, line_num, window=20, stack_trace=stack_sample)
            err["code_context"] = code_snippet

        errors_summary_list.append({
            "id": idx,
            "error_type": err.get("error_type"),
            "error_location": loc,
            "occurrences": err.get("count", 1),
            "role": err.get("role"),
            "error_message": err.get("error_message"),
            "live_code_snippet_around_error": code_snippet or "未檢索到本機程式碼",
            "error_stack_snippet": err.get("error_stack_sample", "")[:400]
        })
        
    errors_json_str = json.dumps(errors_summary_list, ensure_ascii=False, indent=2)
    
    prompt = f"""你是一位資深的 SRE 系統運維架構師與 AI 系統專家。
當前監控的微服務專案為：【{service_name}】。
系統在過去一段時間內發生了以下【多項錯誤日誌】，我們已經透過 SRE 代碼檢索技能，將每個錯誤在 main 分支的出錯行前後代碼提取給你看：

【當前系統捕獲的所有錯誤項目與真實原始碼】
```json
{errors_json_str}
```

請通盤診斷這些問題，比對真實原始碼與錯誤訊息，分析它們之間是否存在相互影響或連鎖因果，並產出一套【跨檔案協同修復計畫】：

請嚴格輸出符合以下 JSON 格式的診斷任務（請直接輸出純 JSON，不要夾帶任何其他文字）：
{{
  "title": "[SRE-Daily] {service_name} 異常全景診斷與協同修復工單 ({datetime.now().strftime('%Y-%m-%d')})",
  "severity": "High / Medium / Low",
  "correlation_analysis": "深入分析這幾個問題之間的連鎖反應、根本源頭與對應程式碼邏輯。",
  "coordinated_fix_plan": "依據上述真實程式碼片段，提供跨檔案協同修補架構與具體代碼修改建議（指出改哪一行、如何改動才能通過全量測試）。",
  "sub_items": [
    "1. 修復檔案 A (說明具體步驟)",
    "2. 修復檔案 B (說明具體步驟)",
    "3. 執行全量 pytest 測試驗證協同修復成果"
  ]
}}
"""

    messages = [
        {"role": "system", "content": f"你是一位專業嚴謹的 SRE AI 運維專家，專精於微服務 [{service_name}] 的全域因果分析與代碼級修補規劃。"},
        {"role": "user", "content": prompt}
    ]

    try:
        # 帶入 SRE 原生技能庫 (Tools)
        response = openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0.2
        )
        
        choice = response.choices[0]
        # 若大模型想主動發動技能 (Tool Calling) 查閱其他檔案
        if choice.message.tool_calls:
            print(f"🔧 大模型主動發動 SRE 技能調用 ({len(choice.message.tool_calls)} 次)...")
            messages.append(choice.message)
            for tool_call in choice.message.tool_calls:
                t_name = tool_call.function.name
                t_args = tool_call.function.arguments
                t_result = execute_skill(t_name, t_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": t_result
                })
            # 第二輪調用獲取最終診斷結果
            second_response = openai_client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                temperature=0.2
            )
            raw_output = second_response.choices[0].message.content.strip()
        else:
            raw_output = choice.message.content.strip() if choice.message.content else "{}"
            
        clean_json = raw_output
        if clean_json.startswith("```"):
            lines = clean_json.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_json = "\n".join(lines).strip()
            
        return json.loads(clean_json)
    except Exception as e:
        print(f"⚠️ 全域 LLM 診斷失敗: {e}")
        return {
            "title": f"[SRE-Daily] 系統異常全景診斷與協同修復工單 ({datetime.now().strftime('%Y-%m-%d')})",
            "severity": "High",
            "correlation_analysis": "系統發生多項異常，包含外部 API 抖動與格式驗證問題。",
            "coordinated_fix_plan": "建議同時針對各出錯檔案加入重試機制與防禦性例外處理。",
            "sub_items": [
                "1. 檢視出錯檔案",
                "2. 實作協同修補邏輯",
                "3. 執行 pytest 驗證"
            ]
        }

def sync_master_report_to_gitlab(report_file_path: str, task_info: dict, project_id: str = None) -> None:
    """
    【歷史防刷與全景工單同步】
    將本地整理完成的全景 Markdown 報告同步發布至 GitLab Issue。
    支援指定微服務專案之 project_id。
    """
    pid = project_id or GITLAB_PROJECT_ID
    if not GITLAB_URL or not GITLAB_TOKEN or not pid:
        print(f"⚠️ 未配置專案 (ID: {pid}) 之 GitLab 環境變數，跳過遠端 Issue 同步。")
        return

    headers = {
        "Private-Token": GITLAB_TOKEN,
        "Content-Type": "application/json"
    }
    
    title = task_info.get("title", f"[SRE-Daily] 系統異常全景診斷與協同修復工單 ({datetime.now().strftime('%Y-%m-%d')})")
    
    try:
        with open(report_file_path, "r", encoding="utf-8") as f:
            md_content = f.read()
    except Exception as e:
        print(f"⚠️ 讀取本地報告檔案失敗: {e}")
        md_content = task_info.get("coordinated_fix_plan", "")

    # 1. 歷史防刷檢查：查詢 GitLab 上是否已有相同標題且未關閉的 Issue
    existing_issue_iid = None
    try:
        check_url = f"{GITLAB_URL}/api/v4/projects/{pid}/issues"
        params = {"state": "opened", "search": datetime.now().strftime('%Y-%m-%d')}
        resp = httpx.get(check_url, headers=headers, params=params, timeout=10.0)
        if resp.status_code == 200:
            existing_issues = resp.json()
            for issue in existing_issues:
                if issue.get("title") == title:
                    existing_issue_iid = issue.get("iid")
                    break
    except Exception as e:
        print(f"⚠️ 查詢 GitLab 現存 Issue 時發生異常: {e}")

    # 2. 如果已存在，原地更新 (PUT) Description；若不存在，新建 (POST)
    if existing_issue_iid:
        print(f"🔄 GitLab 專案 (ID: {pid}) 已存在工單 Issue #{existing_issue_iid}，進行「原地彙整更新」...")
        
        trigger_url = f"{CHATOPS_BASE_URL}/heal?issue_iid={existing_issue_iid}&project_id={pid}"
        chatops_markdown = (
            "\n\n---\n\n"
            "### 🤖 AI 自動協同自癒控制台 (ChatOps)\n"
            f"👉 **[點此一鍵觸發 GitLab CI/CD 協同自癒演練]({trigger_url})**\n"
            "*(點擊後將直接觸發 GitLab Runner 啟動 Pipeline、建立修復分支、跑 pytest 並發起 MR)*"
        )
        full_description = md_content + chatops_markdown
        
        update_url = f"{GITLAB_URL}/api/v4/projects/{pid}/issues/{existing_issue_iid}"
        update_payload = {"description": full_description}
        try:
            update_resp = httpx.put(update_url, headers=headers, json=update_payload, timeout=10.0)
            if update_resp.status_code == 200:
                print(f"✅ 已成功將最新報告原地更新至 GitLab Issue #{existing_issue_iid}！")
            else:
                print(f"⚠️ 更新 GitLab Issue 失敗: {update_resp.status_code} - {update_resp.text}")
        except Exception as e:
            print(f"❌ 更新 GitLab Issue 失敗: {e}")
            
    else:
        # 新建 Issue
        print(f"📝 正在 GitLab 專案 (ID: {pid}) 建立全景協同工單: {title} ...")
        try:
            create_url = f"{GITLAB_URL}/api/v4/projects/{pid}/issues"
            payload = {
                "title": title,
                "description": md_content,
                "labels": "AI-Debug,SRE-Diagnostic,Coordinated-Fix,bug",
                "priority": "high" if task_info.get("severity") == "High" else "medium"
            }
            resp = httpx.post(create_url, headers=headers, json=payload, timeout=10.0)
            if resp.status_code == 201:
                issue_data = resp.json()
                new_iid = issue_data.get("iid")
                web_url = issue_data.get("web_url")
                print(f"✅ 成功在 GitLab 建立任務卡: #{new_iid} {title} ({web_url})")
                
                # 補上一鍵自癒連結
                if new_iid:
                    trigger_url = f"{CHATOPS_BASE_URL}/heal?issue_iid={new_iid}&project_id={pid}"
                    chatops_markdown = (
                        "\n\n---\n\n"
                        "### 🤖 AI 自動協同自癒控制台 (ChatOps)\n"
                        f"👉 **[點此一鍵觸發 GitLab CI/CD 協同自癒演練]({trigger_url})**\n"
                        "*(點擊後將直接觸發 GitLab Runner 啟動 Pipeline、建立修復分支、跑 pytest 並發起 MR)*"
                    )
                    full_description = md_content + chatops_markdown
                    update_url = f"{GITLAB_URL}/api/v4/projects/{pid}/issues/{new_iid}"
                    httpx.put(update_url, headers=headers, json={"description": full_description}, timeout=10.0)
            else:
                print(f"❌ 建立 GitLab 任務卡失敗: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"❌ 呼叫 GitLab 建立 Issue 失敗: {e}")


# ============================================================================
# SRE 每週總體分析大腦與報告生成模組 (Weekly Holistic Analysis)
# ============================================================================

def aggregate_project_weekly_data(service_name: str, hours: int = 168, base_dir: str = "reports") -> dict:
    """
    聚合特定微服務專案在過去 7 天（168 小時）內的日誌數據、環境分佈與歷史每日診斷
    """
    projects_cfg = load_projects_config()
    logs_raw = fetch_recent_logs(hours=hours, level="ERROR")
    matched_logs = [l for l in logs_raw if match_log_to_service(l, projects_cfg) == service_name]
    
    # 1. 每日錯誤走勢統計
    daily_breakdown = {}
    env_breakdown = {}
    for l in matched_logs:
        ts = l.get("@timestamp") or l.get("timestamp") or ""
        day_str = ts[:10] if len(ts) >= 10 else "unknown"
        daily_breakdown[day_str] = daily_breakdown.get(day_str, 0) + 1
        
        env_val = l.get("environment") or l.get("env") or (l.get("service", {}).get("environment") if isinstance(l.get("service"), dict) else None) or "production"
        env_breakdown[str(env_val).lower()] = env_breakdown.get(str(env_val).lower(), 0) + 1

    # 2. 錯誤聚類與切片
    aggregated = aggregate_errors(matched_logs)
    
    # 3. 讀取該專案過去 7 天的每日歷史診斷紀錄
    daily_summaries = []
    proj_dir = os.path.join(base_dir, service_name)
    if os.path.exists(proj_dir):
        for f in sorted(os.listdir(proj_dir), reverse=True):
            if f.startswith("report_") and f.endswith(".md"):
                fpath = os.path.join(proj_dir, f)
                try:
                    with open(fpath, "r", encoding="utf-8") as rf:
                        content = rf.read(1200)
                    sev_m = re.search(r"\*\*整體嚴重度\*\*：\*\*(.*?)\*\*", content)
                    sev = sev_m.group(1).strip() if sev_m else "High"
                    title_m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                    title = title_m.group(1).strip() if title_m else f
                    daily_summaries.append(f"- 報告檔案 {f}: 【嚴重度: {sev}】 {title[:80]}")
                except Exception:
                    pass
                if len(daily_summaries) >= 7:
                    break

    return {
        "service_name": service_name,
        "total_errors": len(matched_logs),
        "distinct_issues_count": len(aggregated),
        "daily_breakdown": daily_breakdown,
        "env_breakdown": env_breakdown,
        "aggregated_errors": aggregated[:15],
        "daily_summaries": daily_summaries
    }

def analyze_project_weekly_holistically(service_name: str, stats: dict, week_str: str) -> dict:
    """
    調用 LLM 大腦針對單一專案進行一週宏觀趨勢、頑疾識別與架構調優分析
    """
    print(f"🧠 正在請求 SRE AI 大腦 ({LLM_MODEL}) 對專案 [{service_name}] 進行第 {week_str} 週總分析...")
    
    err_samples = []
    for idx, e in enumerate(stats.get("aggregated_errors", []), 1):
        loc = e.get("error_location", "unknown:0")
        stack = e.get("error_stack_sample", "") or ""
        snippet = ""
        if ":" in loc:
            parts = loc.split(":")
            fp = parts[0].strip()
            try:
                ln = int(parts[1])
                snippet = fetch_code_snippet(service_name, fp, ln, window=10, stack_trace=stack)
            except Exception:
                pass
        err_samples.append({
            "id": idx,
            "location": loc,
            "type": e.get("error_type"),
            "count_7d": e.get("count", 1),
            "message": e.get("error_message"),
            "code_snippet": snippet[:400] if snippet else "無原始碼切片"
        })
        
    errors_str = json.dumps(err_samples, ensure_ascii=False, indent=2)
    daily_hist_str = "\n".join(stats.get("daily_summaries", [])) if stats.get("daily_summaries") else "本週無每日歷史診斷存檔。"

    prompt = f"""你是一位資深 SRE 系統運維架構師。
當前檢視的微服務專案為：【{service_name}】。
我們剛完成過去 7 天（168 小時）的運維全量指標聚合，以下為第 {week_str} 週的專案數據：

【過去 7 天核心指標】
- 7 天累計異常日誌數：{stats.get("total_errors", 0)} 次
- 獨立問題項目數：{stats.get("distinct_issues_count", 0)} 個
- 每日錯誤分佈：{json.dumps(stats.get("daily_breakdown", {}), ensure_ascii=False)}
- 運行環境分佈：{json.dumps(stats.get("env_breakdown", {}), ensure_ascii=False)}

【過去 7 天高頻問題聚類與代碼切片】
```json
{errors_str}
```

【過去一週每日診斷工單存檔】
{daily_hist_str}

請針對這 7 天的數據進行深度宏觀綜合評估，產出一份具備技術治理深度的每週 SRE 營運綜整報告。
請嚴格輸出符合以下 JSON 格式的內容（直接輸出純 JSON，不要夾帶任何其他文字）：
{{
  "title": "[SRE-Weekly] {service_name} 週度營運穩定度與架構綜整報告 ({week_str})",
  "severity": "Normal / Warning / Critical",
  "trend_summary": "深入分析過去一週系統的穩定度走勢、波峰波谷發生時間點與環境穩定度差異總結。",
  "recurring_issues": [
    {{
      "location": "app/services/xxx.py:120",
      "description": "說明此錯誤在過去一週反覆出現的規律與根本原因",
      "impact": "對業務交易或系統可用性的實質衝擊"
    }}
  ],
  "action_items": [
    "1. 下週優先架構調優動作（例如加強連線池、重試或快取）",
    "2. 針對頑疾代碼的徹底重構計畫",
    "3. 監控指標與告警閾值優化"
  ]
}}
"""

    messages = [
        {"role": "system", "content": f"你是一位精通微服務架構與系統高可用的 SRE 總架構師，專精於 [{service_name}] 的營運健康評估與週度技術治理。"},
        {"role": "user", "content": prompt}
    ]

    try:
        response = openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.2
        )
        raw_output = response.choices[0].message.content.strip()
        clean_json = raw_output
        if clean_json.startswith("```"):
            lines = clean_json.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_json = "\n".join(lines).strip()
        return json.loads(clean_json)
    except Exception as e:
        print(f"⚠️ 週度 LLM 專案分析失敗: {e}")
        sev = "Critical" if stats.get("total_errors", 0) > 50 else ("Warning" if stats.get("total_errors", 0) > 0 else "Normal")
        return {
            "title": f"[SRE-Weekly] {service_name} 週度營運穩定度與架構綜整報告 ({week_str})",
            "severity": sev,
            "trend_summary": f"專案過去 7 天累計發生 {stats.get('total_errors', 0)} 次異常日誌，涵蓋 {stats.get('distinct_issues_count', 0)} 個獨立問題類型。",
            "recurring_issues": [
                {
                    "location": "系統常規日誌",
                    "description": "需持續關注外部連線與資源釋放",
                    "impact": "可能導致暫時性網路延遲或回應超時"
                }
            ],
            "action_items": [
                "1. 檢視日誌集中管理之 ERROR 等級項目",
                "2. 評估資料庫與外部 API 之連線池設定",
                "3. 維持 CI/CD 自動化測試覆蓋率"
            ]
        }

def generate_project_weekly_report(service_name: str, week_str: str = None, base_dir: str = "reports", send_email: bool = True) -> str:
    """
    完整執行單一微服務專案的每週綜整報告生成流程
    """
    w_str = week_str or get_current_week_str()
    # 確保代碼鏡像為最新
    sync_repo(service_name)
    
    # 1. 聚合數據
    stats = aggregate_project_weekly_data(service_name, hours=168, base_dir=base_dir)
    
    # 2. AI 診斷
    ai_analysis = analyze_project_weekly_holistically(service_name, stats, w_str)
    
    # 3. 儲存週報 Markdown
    report_file = save_project_weekly_report(service_name, stats, ai_analysis, w_str, base_dir=base_dir)
    print(f"📄 專案 [{service_name}] 第 {w_str} 週週報已產生: {report_file}")
    
    # 4. 發送專屬通知郵件
    if send_email:
        title = ai_analysis.get("title", f"{service_name} 第 {w_str} 週 SRE 營運週報")
        report_url = f"{CHATOPS_BASE_URL}/weekly?service={service_name}&week={w_str}"
        send_weekly_email_notification(title, service_name, ai_analysis, report_url=report_url)
        
    return report_file

def aggregate_global_weekly_data(hours: int = 168, base_dir: str = "reports") -> dict:
    """
    聚合全微服務系統在過去 7 天內的總體營運指標與各專案排行榜
    """
    projects_cfg = load_projects_config()
    logs_raw = fetch_recent_logs(hours=hours, level="ERROR")
    
    # 依專案分類
    proj_error_counts = {}
    proj_envs = {}
    for l in logs_raw:
        sname = match_log_to_service(l, projects_cfg)
        proj_error_counts[sname] = proj_error_counts.get(sname, 0) + 1
        
        env_val = l.get("environment") or l.get("env") or "production"
        if sname not in proj_envs:
            proj_envs[sname] = {}
        proj_envs[sname][str(env_val).lower()] = proj_envs[sname].get(str(env_val).lower(), 0) + 1

    rankings = []
    # 包含所有註冊的專案（即使 0 錯誤）
    all_names = set(list(projects_cfg.keys()) + list(proj_error_counts.keys()))
    for sname in all_names:
        cnt = proj_error_counts.get(sname, 0)
        sev = "Critical" if cnt >= 50 else ("Warning" if cnt > 0 else "Normal")
        envs_dict = proj_envs.get(sname, {})
        top_env = max(envs_dict, key=envs_dict.get) if envs_dict else "production"
        rankings.append({
            "service_name": sname,
            "errors_7d": cnt,
            "severity": sev,
            "top_env": top_env
        })
        
    rankings.sort(key=lambda x: x["errors_7d"], reverse=True)
    
    return {
        "total_services": len(all_names),
        "total_system_errors": len(logs_raw),
        "service_rankings": rankings
    }

def analyze_global_weekly_holistically(all_stats: dict, week_str: str) -> dict:
    """
    調用 LLM 大腦站在全域 SRE 總架構師角度進行全系統週度評估
    """
    print(f"🧠 正在請求 SRE AI 大腦 ({LLM_MODEL}) 對【全系統微服務】進行第 {week_str} 週全域總分析...")
    
    rankings_json = json.dumps(all_stats.get("service_rankings", []), ensure_ascii=False, indent=2)
    prompt = f"""你是一位微服務系統 SRE 總架構師與技術主管。
以下是過去 7 天（168 小時）全系統所有微服務專案的綜合統計數據：

【受監控微服務數量】：{all_stats.get("total_services", 0)} 個專案
【全系統 7 天累計錯誤總數】：{all_stats.get("total_system_errors", 0)} 次
【各微服務故障頻率與健康排行】：
```json
{rankings_json}
```

請站在全域 SRE 總架構師視角，綜觀分析本週全微服務系統的健康狀況，產出一份全域總控營運週報。
請嚴格輸出符合以下 JSON 格式的內容（直接輸出純 JSON，不要夾帶任何其他文字）：
{{
  "title": "🌐 [SRE 全域總體週報] 全微服務系統營運與架構健康評估 ({week_str})",
  "severity": "Healthy / Warning / Critical",
  "executive_summary": "全系統一週營運綜整概述，包含整體穩定度指標與最需要關注的瓶頸服務。",
  "cascading_analysis": "分析各微服務之間的相依性、調用鏈與連鎖反應風險（例如共用資源瓶頸、網路抖動或第三方相依問題）。",
  "strategic_recommendations": [
    "1. 全域架構調優重點 1",
    "2. 全域架構調優重點 2",
    "3. 資源配置與容量規劃建議"
  ]
}}
"""

    messages = [
        {"role": "system", "content": "你是一位頂尖的 SRE 總架構師，專精於分散式微服務叢集的可觀測性治理與高可用架構演進。"},
        {"role": "user", "content": prompt}
    ]

    try:
        response = openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.2
        )
        raw_output = response.choices[0].message.content.strip()
        clean_json = raw_output
        if clean_json.startswith("```"):
            lines = clean_json.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_json = "\n".join(lines).strip()
        return json.loads(clean_json)
    except Exception as e:
        print(f"⚠️ 全域週度 LLM 分析失敗: {e}")
        tot_err = all_stats.get("total_system_errors", 0)
        sev = "Critical" if tot_err > 100 else ("Warning" if tot_err > 0 else "Healthy")
        return {
            "title": f"🌐 [SRE 全域總體週報] 全微服務系統營運與架構健康評估 ({week_str})",
            "severity": sev,
            "executive_summary": f"全域共監控 {all_stats.get('total_services', 0)} 個微服務專案，過去一週累計檢測到 {tot_err} 筆異常日誌。",
            "cascading_analysis": "各微服務在相依調用與資料庫交互時需保持重試防護與降級備援機制。",
            "strategic_recommendations": [
                "1. 針對累計異常較高之專案安排架構優化",
                "2. 檢視全域連線池與微服務調用超時設定",
                "3. 持續強化跨專案可觀測性監控覆蓋"
            ]
        }

def generate_global_weekly_report(week_str: str = None, base_dir: str = "reports", send_email: bool = True) -> str:
    """
    完整執行全域總控週報生成流程
    """
    w_str = week_str or get_current_week_str()
    # 1. 聚合全域數據
    all_stats = aggregate_global_weekly_data(hours=168, base_dir=base_dir)
    
    # 2. AI 宏觀診斷
    ai_global_analysis = analyze_global_weekly_holistically(all_stats, w_str)
    
    # 3. 儲存週報 Markdown
    report_file = save_global_weekly_report(all_stats, ai_global_analysis, w_str, base_dir=base_dir)
    print(f"🌐 全域總控第 {w_str} 週週報已產生: {report_file}")
    
    # 4. 發送全域管理通知郵件
    if send_email:
        title = ai_global_analysis.get("title", f"全系統第 {w_str} 週 SRE 總體營運週報")
        report_url = f"{CHATOPS_BASE_URL}/weekly?week={w_str}"
        send_weekly_email_notification(title, None, ai_global_analysis, report_url=report_url)
        
    return report_file


def main(hours: int = 24, index_name: str = None):
    reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_base_dir, exist_ok=True)
    
    print("\n" + "="*70)
    print("🚀 SRE Sentinel: 多專案代碼鏡像 ➡️ 原生 Skills 切片 ➡️ 分層報告 ➡️ GitLab 協同")
    print(f"📅 掃描範圍：過去 {hours} 小時 ｜ 本地報告根目錄：{reports_base_dir}")
    print("="*70 + "\n")
    
    # 1. 撈取 Elasticsearch ERROR 日誌
    error_logs = fetch_recent_logs(hours=hours, level="ERROR", index_name=index_name)
    
    if error_logs:
        # 2. 依微服務專案進行智能分流 (自訂規則優先 ＋ 萬能相容回退)
        projects_cfg = load_projects_config()
        service_groups: dict[str, list[dict]] = {}
        for log in error_logs:
            sname = match_log_to_service(log, projects_cfg)
            if sname not in service_groups:
                service_groups[sname] = []
            service_groups[sname].append(log)
            
        print(f"🌐 偵測到 {len(service_groups)} 個微服務專案發生錯誤：{list(service_groups.keys())}\n")
        
        for sname, logs in service_groups.items():
            print(f"\n--- 📦 開始處理專案 [{sname}] (共 {len(logs)} 筆日誌) ---")
            
            # (A) 自動同步該專案 main 分支鏡像
            sync_repo(sname)
            proj_conf = get_project_config(sname)
            proj_id = str(proj_conf.get("project_id")) if proj_conf and proj_conf.get("project_id") else GITLAB_PROJECT_ID

            # (B) 內容去重聚類
            aggregated = aggregate_errors(logs)
            print(f"🔍 聚類後獲得 {len(aggregated)} 個獨立問題項目。")
            
            # (B-1) 異常生命週期追蹤與指紋比對
            lifecycle_summary = issue_tracker.update_lifecycle(sname, aggregated)
            print(f"🧬 生命週期追蹤: 專案 [{sname}] 發現全新問題 {lifecycle_summary['new_count']} 個，連續頑疾 {lifecycle_summary['chronic_count']} 個 (最長持續 {lifecycle_summary['max_consecutive_days']} 天)")

            # (C) 呼叫大腦進行原生技能分析與代碼切片 (支援 Token 快取重用)
            ai_analysis = analyze_all_errors_holistically(aggregated, service_name=sname, lifecycle_summary=lifecycle_summary)
            # 更新快取
            issue_tracker.update_cached_analysis(sname, ai_analysis)
            
            # (D) 產出專案分層報告 reports/<service_name>/report_YYYYMMDD.md
            report_file = save_or_update_master_report(
                aggregated_errors=aggregated,
                ai_global_analysis=ai_analysis,
                service_name=sname,
                base_dir=reports_base_dir
            )
            print(f"📄 專案報告落盤完成: {report_file}")
            
            # (E) 同步至該專案 GitLab Issue
            sync_master_report_to_gitlab(report_file, ai_analysis, project_id=proj_id)

            # (F) 智慧 Email 告警推播 (具備舊疾自動靜音與惡化警報)
            has_prod = any("prod" in str(l.get("environment") or "").lower() for l in logs)
            should_send, reason = issue_tracker.should_send_email_alert(sname, lifecycle_summary, has_prod_error=has_prod)
            if should_send:
                print(f"📧 [Email 觸發告警] {reason}，發送通知郵件至負責人...")
                report_web_url = f"{CHATOPS_BASE_URL}/report/{sname}/{os.path.basename(report_file)}"
                send_alert_notification(sname, ai_analysis, report_url=report_web_url)
            else:
                print(f"🔕 [Email 智慧降噪] {reason}")

        # 3. 更新全域多專案總覽看板 index.md
        index_path = update_reports_index_dashboard(base_dir=reports_base_dir)
        print("\n" + "="*70)
        print("🎉 全域多專案分析與工單同步完成！")
        print(f"📊 SRE Sentinel 健康看板：file:///{index_path.replace(os.sep, '/')}")
        print("="*70 + "\n")
        
    else:
        print("🎉 過去時間內無任何錯誤日誌，系統健康狀態良好！")
        update_reports_index_dashboard(base_dir=reports_base_dir)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SRE AI Agent: Holistic Batch Analysis & Coordinated Self-Healing")
    parser.add_argument("--mode", type=str, choices=["web", "server", "scan"], default=None, help="運行模式: web (網頁控制台/探測器), scan (執行日誌掃描診斷)")
    parser.add_argument("--hours", type=int, default=24, help="掃描過去 X 小時的日誌 (預設 24 小時)")
    parser.add_argument("--index", type=str, default=None, help="指定 Elasticsearch Index (預設 logs-*)")
    parser.add_argument("--server", action="store_true", help="啟動 ChatOps / Web 儀表板伺服器")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help=f"伺服器監聽 Port (預設 {DEFAULT_SERVER_PORT})")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="伺服器監聽 Host (預設 0.0.0.0)")
    
    args = parser.parse_args()
    
    # 支援 --mode web / --mode server 或舊有的 --server 參數
    if args.mode in ("web", "server") or args.server:
        import uvicorn
        print(f"🚀 正在啟動 SRE AI Sentinel 網頁控制台與日誌探測器，監聽 http://{args.host}:{args.port} ...")
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        main(hours=args.hours, index_name=args.index)
