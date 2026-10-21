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
from fastapi.responses import HTMLResponse, Response

from core.report_generator import (
    save_or_update_master_report,
    update_reports_index_dashboard,
    update_project_summary
)
from core.web_dashboard import (
    get_services_health,
    render_dashboard_html,
    render_report_view_html,
    render_log_explorer_html
)
from core.repo_manager import (
    sync_repo,
    get_project_config,
    load_projects_config,
    save_projects_config,
    add_or_update_project,
    delete_project,
    fetch_gitlab_branches
)
from pydantic import BaseModel
from skills import TOOLS_SCHEMA, execute_skill
from skills.code_tools import fetch_code_snippet
from core.issue_tracker import tracker as issue_tracker
from core.database import init_db

# 🛡️ 初始化資料庫與執行平滑遷移
init_db()

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

def send_alert_notification(service_name: str, task_info: dict, report_url: str = None, aggregated_errors: list = None) -> bool:
    """
    發送異常告警通知 (支援 Email HTML 郵件與 Discord/Slack Webhook)
    支援專案級獨立收件人 (notification_emails)，若無則回退至全域 EMAIL_RECEIVER
    信件內容聚焦於：連鎖因果分析與具體異常錯誤清單 (Error Messages)，不再冗長贅述修復建議。
    """
    title = task_info.get("title", f"[SRE-Daily] {service_name} 異常告警")
    severity = task_info.get("severity", "High")
    correlation = task_info.get("correlation_analysis", "系統檢測到異常日誌")
    
    # 優先讀取該專案自訂的收件人，若未設定則使用全域 EMAIL_RECEIVER
    proj_conf = get_project_config(service_name) or {}
    target_emails = proj_conf.get("notification_emails") or EMAIL_RECEIVER
    
    # 組裝具體異常錯誤訊息清單 (Error Messages Top 5)
    errors_html = ""
    raw_errors = aggregated_errors or task_info.get("errors") or []
    if raw_errors:
        err_items = []
        for idx, err in enumerate(raw_errors[:5]):
            ename = str(err.get("error_type") or "UnknownError")
            eloc = str(err.get("error_location") or "unknown:0")
            ecnt = err.get("count", 1)
            raw_msg = str(err.get("raw_error") or err.get("message") or err.get("log") or "").strip()
            # 若無明確 raw_error，取原始 log 摘錄
            if not raw_msg and err.get("samples"):
                first_s = err["samples"][0]
                raw_msg = str(first_s.get("message") or first_s.get("log") or first_s.get("error") or "")
            raw_msg = (raw_msg[:180] + "...") if len(raw_msg) > 180 else (raw_msg or ename)
            raw_msg_escaped = raw_msg.replace("<", "&lt;").replace(">", "&gt;")

            status_tag = ""
            if err.get("is_new"):
                status_tag = '<span style="background: #ffe4e6; color: #be123c; font-size: 10px; font-weight: bold; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">🆕 新問題</span>'
            elif err.get("is_ongoing"):
                status_tag = f'<span style="background: #fef3c7; color: #92400e; font-size: 10px; font-weight: bold; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">⏳ 連續頑疾 ({err.get("consecutive_days", 2)}天)</span>'

            err_items.append(f"""
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; margin-bottom: 10px;">
                <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
                    <div style="font-size: 13px; font-weight: 700; color: #0f172a; font-family: monospace;">
                        {idx + 1}. {ename}{status_tag}
                    </div>
                    <span style="background: #ffffff; border: 1px solid #cbd5e1; font-size: 11px; font-weight: 600; color: #475569; padding: 2px 8px; border-radius: 6px;">
                        今日 {ecnt} 次
                    </span>
                </div>
                <div style="font-size: 11px; color: #64748b; font-family: monospace; margin-bottom: 4px;">
                    📍 代碼位置：<span style="color: #334155; font-weight: 600;">{eloc}</span>
                </div>
                <div style="font-size: 11px; color: #dc2626; background: #ffffff; border: 1px dashed #fca5a5; padding: 6px 10px; border-radius: 6px; font-family: monospace; word-break: break-all;">
                    💬 {raw_msg_escaped}
                </div>
            </div>
            """)
        errors_html = f"""
        <div style="margin-bottom: 24px;">
            <h4 style="color: #334155; margin: 0 0 10px 0; font-size: 14px;">🚨 具體異常錯誤清單 (Top Errors)：</h4>
            {"".join(err_items)}
        </div>
        """

    email_sent = False
    # 1. 發送 Email 通知
    if SMTP_USER and SMTP_PASSWORD and target_emails:
        try:
            # 支援逗號或分號分隔多個收件人
            recipients = [e.strip() for e in re.split(r"[,;]", target_emails) if e.strip()]
            
            # 依據嚴重度決定配色風格
            sev_upper = severity.upper()
            if sev_upper in ("CRITICAL", "HIGH"):
                header_gradient = "linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)"
                badge_bg = "#fee2e2"
                badge_color = "#991b1b"
                badge_border = "#fca5a5"
                status_icon = "🚨"
            elif sev_upper in ("MEDIUM", "WARNING"):
                header_gradient = "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)"
                badge_bg = "#fef3c7"
                badge_color = "#92400e"
                badge_border = "#fcd34d"
                status_icon = "⚠️"
            else:
                header_gradient = "linear-gradient(135deg, #10b981 0%, #059669 100%)"
                badge_bg = "#d1fae5"
                badge_color = "#065f46"
                badge_border = "#6ee7b7"
                status_icon = "ℹ️"

            # 總錯誤次數與獨立問題數
            tot_err_count = sum(e.get("count", 1) for e in raw_errors)
            distinct_count = len(raw_errors)
            
            html_body = f"""
            <!DOCTYPE html>
            <html lang="zh-TW">
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>[SRE Sentinel] {service_name} 異常告警</title>
            </head>
            <body style="margin: 0; padding: 0; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; color: #334155;">
                <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: #0f172a; padding: 32px 12px;">
                    <tr>
                        <td align="center">
                            <!-- Main Container Card -->
                            <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 640px; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.4), 0 10px 10px -5px rgba(0, 0, 0, 0.2);">
                                
                                <!-- Top Header Banner -->
                                <tr>
                                    <td style="background: {header_gradient}; padding: 32px 32px 28px 32px; color: #ffffff;">
                                        <table width="100%" border="0" cellspacing="0" cellpadding="0">
                                            <tr>
                                                <td>
                                                    <div style="display: inline-block; background-color: rgba(255,255,255,0.2); backdrop-filter: blur(4px); padding: 4px 12px; border-radius: 9999px; font-size: 11px; font-weight: 800; letter-spacing: 0.8px; text-transform: uppercase;">
                                                        {status_icon} SRE SENTINEL ALERT
                                                    </div>
                                                </td>
                                                <td align="right">
                                                    <span style="font-size: 12px; opacity: 0.9; font-weight: 500;">
                                                        {datetime.now().strftime('%Y-%m-%d %H:%M')}
                                                    </span>
                                                </td>
                                            </tr>
                                            <tr>
                                                <td colspan="2" style="padding-top: 14px;">
                                                    <h1 style="margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.3px; line-height: 1.3;">
                                                        {service_name}
                                                    </h1>
                                                    <p style="margin: 6px 0 0 0; font-size: 13px; opacity: 0.95; line-height: 1.5;">
                                                        系統自動巡檢捕獲到服務異常日誌，已觸發告警通知。
                                                    </p>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>

                                <!-- Metrics Stats Row -->
                                <tr>
                                    <td style="background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; padding: 16px 32px;">
                                        <table width="100%" border="0" cellspacing="0" cellpadding="0">
                                            <tr>
                                                <td width="33%" align="center" style="border-right: 1px solid #e2e8f0;">
                                                    <div style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase;">嚴重等級</div>
                                                    <div style="margin-top: 4px; display: inline-block; background: {badge_bg}; color: {badge_color}; border: 1px solid {badge_border}; font-size: 12px; font-weight: 800; padding: 2px 10px; border-radius: 6px;">
                                                        {severity.upper()}
                                                    </div>
                                                </td>
                                                <td width="33%" align="center" style="border-right: 1px solid #e2e8f0;">
                                                    <div style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase;">獨立問題數</div>
                                                    <div style="margin-top: 4px; font-size: 16px; font-weight: 800; color: #0f172a;">
                                                        {distinct_count} <span style="font-size: 11px; font-weight: normal; color: #64748b;">個</span>
                                                    </div>
                                                </td>
                                                <td width="33%" align="center">
                                                    <div style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase;">本日異常量</div>
                                                    <div style="margin-top: 4px; font-size: 16px; font-weight: 800; color: #ef4444;">
                                                        {tot_err_count} <span style="font-size: 11px; font-weight: normal; color: #64748b;">次</span>
                                                    </div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>

                                <!-- Content Body -->
                                <tr>
                                    <td style="padding: 28px 32px 32px 32px;">
                                        
                                        <!-- Root Cause / Correlation Analysis -->
                                        <div style="margin-bottom: 26px;">
                                            <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 10px;">
                                                <span style="font-size: 15px;">🧠</span>
                                                <h3 style="margin: 0; font-size: 14px; font-weight: 800; color: #0f172a; text-transform: uppercase; letter-spacing: 0.3px;">
                                                    AI 關聯與因果剖析
                                                </h3>
                                            </div>
                                            <div style="background-color: #f8fafc; border-left: 4px solid #6366f1; border-top: 1px solid #e2e8f0; border-right: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; border-radius: 0 10px 10px 0; padding: 16px; font-size: 13px; color: #1e293b; line-height: 1.65;">
                                                {correlation}
                                            </div>
                                        </div>

                                        <!-- Top Errors Breakdown -->
                                        {errors_html}

                                        <!-- CTA Action Buttons -->
                                        <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #f1f5f9; text-align: center;">
                                            <a href="{report_url or CHATOPS_BASE_URL}" target="_blank" style="display: inline-block; background: #4f46e5; color: #ffffff; text-decoration: none; padding: 14px 32px; border-radius: 10px; font-weight: 700; font-size: 14px; letter-spacing: 0.2px; box-shadow: 0 4px 6px -1px rgba(79, 70, 229, 0.3);">
                                                🚀 開啟 SRE Sentinel 控制台查看詳情 →
                                            </a>
                                            <div style="margin-top: 10px;">
                                                <a href="{CHATOPS_BASE_URL}/logs?service={service_name}" target="_blank" style="color: #6366f1; text-decoration: none; font-size: 12px; font-weight: 600;">
                                                    🔍 前往即時日誌探測器 (Log Explorer) 篩選
                                                </a>
                                            </div>
                                        </div>

                                    </td>
                                </tr>

                                <!-- Footer -->
                                <tr>
                                    <td style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 18px 32px; text-align: center;">
                                        <p style="margin: 0; font-size: 11px; color: #94a3b8; line-height: 1.5;">
                                            此信件由 <strong>SRE AI Sentinel</strong> 智慧監控 Agent 自動發出。<br>
                                            若欲調整本微服務告警頻率或靜音，請至控制台「⚙️ 專案設定」調整。
                                        </p>
                                    </td>
                                </tr>

                            </table>
                        </td>
                    </tr>
                </table>
            </body>
            </html>
            """
            msg = MIMEText(html_body, 'html', 'utf-8')
            msg['Subject'] = Header(f"🚨 [{severity.upper()}] SRE Sentinel: {service_name} 異常告警通知", 'utf-8')
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
            print(f"📧 成功發送高質感異常告警郵件至 {recipients}")
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
    """
    動態報告渲染器：
    優先從資料庫 Diagnosis 讀取 Markdown 渲染；若請求 summary.md 則動態即時產生，
    完全不依賴硬碟上是否真實存在實體 .md 檔案。
    """
    md_content = None

    # 1. 處理活體總表 summary.md
    if filename == "summary.md":
        try:
            from core.report_generator import update_project_summary
            # 動態由資料庫聚合最新狀態
            temp_path = update_project_summary(service_name=service_name, aggregated_errors=[], ai_analysis={"severity": "Normal"})
            if os.path.exists(temp_path):
                with open(temp_path, "r", encoding="utf-8") as f:
                    md_content = f.read()
        except Exception as e:
            print(f"[ViewReport] generate dynamic summary.md failed: {e}")

    # 2. 處理每日報告 report_YYYYMMDD.md
    elif filename.startswith("report_") and filename.endswith(".md"):
        date_part = filename.replace("report_", "").replace(".md", "")
        # 將 YYYYMMDD 格式化為 YYYY-MM-DD
        if len(date_part) == 8 and date_part.isdigit():
            date_fmt = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
            try:
                from core.database import get_db_session, Diagnosis
                session = get_db_session()
                diag = session.query(Diagnosis).filter_by(service_name=service_name, report_date=date_fmt).first()
                if diag and diag.markdown_content:
                    md_content = diag.markdown_content
                elif diag and diag.root_cause_summary:
                    # 若為舊版遷移無完整 markdown，即時組裝簡潔診斷
                    sub_items = json.loads(diag.sub_items_json) if diag.sub_items_json else []
                    chk = "\n".join(f"- [ ] {it}" for it in sub_items)
                    md_content = f"# {diag.title}\n\n> **日期**：{diag.report_date} ｜ **等級**：{diag.severity}\n\n## 🔍 問題分析\n{diag.root_cause_summary}\n\n## 🛠️ 修復建議\n{diag.coordinated_fix_plan}\n\n### 📋 驗證清單\n{chk}\n"
                session.close()
            except Exception as dbe:
                print(f"[ViewReport] query DB diagnosis failed: {dbe}")

    # 3. 降級讀取實體檔案 (若上述未取得)
    if not md_content:
        reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        file_path = os.path.join(reports_base_dir, service_name, filename)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    md_content = f.read()
            except Exception:
                pass

    if not md_content:
        return HTMLResponse(content="<div style='font-family: sans-serif; padding: 40px; text-align: center;'><h2>❌ 找不到該診斷報告或資料庫紀錄</h2><p><a href='/'>返回儀表板</a></p></div>", status_code=404)

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
    
    sample_errors = [
        {
            "error_type": "Slow query found (time: 3250ms)",
            "error_location": "app/db/query.py:88",
            "count": 12,
            "raw_error": "Query execution time exceeded threshold (3250ms): SELECT * FROM logs WHERE id = $1",
            "is_ongoing": True,
            "consecutive_days": 3
        },
        {
            "error_type": "QueryFailedError: relation 'users' does not exist",
            "error_location": "src/services/user.service.ts:42",
            "count": 2,
            "raw_error": "error: relation 'users' does not exist at character 24",
            "is_new": True
        }
    ]
    test_task = {
        "title": f"[SRE-Test] 系統告警測試信 ({sname})",
        "severity": "High",
        "correlation_analysis": f"這是一封來自 SRE AI Sentinel 針對專案 [{sname}] 的測試告警信。信件已移除修復建議，改為突顯具體出錯訊息與連鎖因果分析。",
    }
    success = send_alert_notification(sname, test_task, report_url=CHATOPS_BASE_URL, aggregated_errors=sample_errors)
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
    email_alert_enabled: bool = True

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
        "notification_emails": req.notification_emails.strip(),
        "email_alert_enabled": req.email_alert_enabled
    }
    success = add_or_update_project(sname, conf)
    if success:
        # 在背景自動執行 git clone / fetch 同步至 repos/ 資料夾，不卡住前端請求
        background_tasks.add_task(sync_repo, sname)
        return {"status": "ok", "message": f"專案 [{sname}] 成功儲存，已在背景自動同步最新程式碼"}
    return {"status": "error", "message": "寫入 projects.json 失敗"}

@app.get("/api/projects/export")
async def api_export_projects():
    """
    一鍵匯出當前所有受監控微服務設定檔 (projects_backup_YYYYMMDD.json 下載)
    """
    configs = load_projects_config()
    content = json.dumps(configs, ensure_ascii=False, indent=2)
    today_str = datetime.now().strftime("%Y%m%d")
    filename = f"projects_backup_{today_str}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

@app.post("/api/projects/{service_name}/toggle_email")
async def api_toggle_project_email(service_name: str):
    """
    一鍵切換該微服務之 Email 告警通知狀態 (啟用/靜音)
    """
    sname = service_name.strip()
    configs = load_projects_config()
    if sname not in configs:
        # 若為尚未在 projects.json 正式登記的專案，自動註冊並更新狀態
        configs[sname] = {
            "project_path": "",
            "project_id": None,
            "branch": "main",
            "description": "微服務應用程式",
            "log_field": "",
            "log_value": "",
            "notification_emails": "",
            "email_alert_enabled": False  # 首次切換直接置為 False (靜音)
        }
        current_state = False
    else:
        current_state = configs[sname].get("email_alert_enabled", True)
        configs[sname]["email_alert_enabled"] = not current_state
        current_state = not current_state

    saved = save_projects_config(configs)
    if saved:
        state_label = "開啟" if current_state else "已靜音"
        return {"status": "ok", "email_alert_enabled": current_state, "message": f"專案 [{sname}] 異常告警信已切換為：{state_label}"}
    return {"status": "error", "message": "儲存專案配置失敗"}

@app.delete("/api/projects/{service_name}")
async def api_delete_project(service_name: str):
    success = delete_project(service_name)
    if success:
        return {"status": "ok", "message": f"專案 [{service_name}] 已自監控清單移除"}
    return {"status": "error", "message": f"找不到專案 [{service_name}] 或移除失敗"}


class AcknowledgeRequest(BaseModel):
    service_name: str
    fingerprint: str
    note: str = ""

@app.post("/api/issues/acknowledge")
async def api_acknowledge_issue(req: AcknowledgeRequest):
    """
    一鍵標記特定錯誤指紋為已知待排期 / 暫不處理：
    1. 狀態變更為 acknowledged
    2. 自動啟動 Email 靜音保護，不再寄信騷擾負責人
    3. 歸入 summary.md 的專案暫緩技術債清單
    """
    sname = req.service_name.strip()
    fp = req.fingerprint.strip()
    note = req.note.strip()
    if not sname or not fp:
        return {"status": "error", "message": "微服務專案名稱與指紋不可為空"}
    
    ok = issue_tracker.acknowledge_issue(sname, fp, note=note)
    if ok:
        reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        update_project_summary(service_name=sname, aggregated_errors=[], ai_analysis={}, base_dir=reports_base_dir)
        return {"status": "ok", "message": f"問題 [{fp}] 已成功標記為已知待排期，後續 Email 告警已自動靜音！"}
    return {"status": "error", "message": f"找不到指定問題指紋 [{fp}]"}

@app.post("/api/issues/unacknowledge")
async def api_unacknowledge_issue(req: AcknowledgeRequest):
    """
    取消已知標記，恢復正常警報與追蹤
    """
    sname = req.service_name.strip()
    fp = req.fingerprint.strip()
    if not sname or not fp:
        return {"status": "error", "message": "微服務專案名稱與指紋不可為空"}
    
    ok = issue_tracker.unacknowledge_issue(sname, fp)
    if ok:
        reports_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        update_project_summary(service_name=sname, aggregated_errors=[], ai_analysis={}, base_dir=reports_base_dir)
        return {"status": "ok", "message": f"問題 [{fp}] 已恢復正常告警追蹤！"}
    return {"status": "error", "message": f"找不到指定問題指紋 [{fp}]"}


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

def normalize_error_signature(text: str) -> str:
    """
    高精度保守正規化：
    針對動態跳動數值（如耗時毫秒、IP、UUID、數字ID）進行標準化遮罩，
    同時嚴格保留英文單詞語意（如 Slow query found, AES Decryption Failed 等），
    避免將 102 筆因微小時間差切碎成 102 個獨立問題。
    """
    if not text:
        return ""
    # 1. 替換時間/時長：time: 187.04ms -> time: <duration>
    s = re.sub(r"time:\s*\d+(\.\d+)?\s*(ms|s|sec)?", "time: <duration>", text, flags=re.IGNORECASE)
    s = re.sub(r"\b\d+(\.\d+)?\s*(ms|s|sec)\b", "<duration>", s, flags=re.IGNORECASE)
    # 2. 替換 IP 位址：192.168.1.1 -> <ip>
    s = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<ip>", s)
    # 3. 替換 UUID
    s = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<uuid>", s)
    # 4. 替換 5 位數以上的純數字 ID（如訂單號、流水號）
    s = re.sub(r"\b\d{5,}\b", "<id>", s)
    return s.strip()

def aggregate_errors(logs: list[dict]) -> list[dict]:
    """
    對 Error Log 進行高精度聚類去重：
    1. 錨定 error_location（檔案:行號）為物理隔離
    2. 提取 error_type 並進行正規化遮罩
    3. 若無 error_type 或為通用 Exception，取 error_message 首行骨架簽名
    4. 依 (location, signature) 聚類，並保留首筆未遮罩之原始 Log 作為展示樣例
    """
    aggregated = {}
    for log in logs:
        location = str(log.get("error_location") or "unknown:0").strip()
        raw_type = str(log.get("error_type") or "").strip()
        raw_msg = str(log.get("error_message") or "").strip()
        
        # 決定錯誤特徵簽名
        norm_type = normalize_error_signature(raw_type)
        first_line_msg = raw_msg.split("\n")[0].strip() if raw_msg else ""
        norm_msg_sig = normalize_error_signature(first_line_msg)
        
        # 判斷 type 是否有效（非空且非通用 Unknown）
        if norm_type and norm_type.lower() not in ("unknown", "unknown_type", "error", "exception"):
            signature = norm_type
        elif norm_msg_sig:
            signature = norm_msg_sig
        else:
            signature = "UnknownError"
            
        key = (location, signature)
        err_stack = str(log.get("error_stack") or "")[:800]
        
        if key not in aggregated:
            aggregated[key] = {
                "error_location": location,
                "error_type": signature,
                "raw_error_type": raw_type or signature,
                "error_signature": signature,
                "error_message": raw_msg,
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
        raw_cause = first_cache.get("root_cause_summary") or first_cache.get("correlation_analysis") or "同前次分析結論，根本原因為外部服務連線或資源瓶頸。"
        clean_cause = re.sub(r'^(【⏳\s*連續頑疾持續追蹤中.*?】\s*)+', '', raw_cause, flags=re.MULTILINE).strip()
        final_cause = f"【⏳ 連續頑疾持續追蹤中（已連續 {consecutive_d} 天）】\n" + clean_cause
        return {
            "title": f"[SRE-Daily] {service_name} 異常全景診斷與協同修復工單 ({datetime.now().strftime('%Y-%m-%d')})",
            "severity": first_cache.get("severity", "High"),
            "correlation_analysis": final_cause,
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
  "title": "📝 {service_name} 每日異常診斷 ({datetime.now().strftime('%Y-%m-%d')})",
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

            # (D-2) 更新專案單一活體總結報告 reports/<service_name>/summary.md (含 7 天滾動趨勢矩陣與技術債字典)
            summary_file = update_project_summary(
                service_name=sname,
                aggregated_errors=aggregated,
                ai_analysis=ai_analysis,
                base_dir=reports_base_dir
            )
            print(f"📋 專案活體總結更新完成: {summary_file}")
            
            # (E) 同步至該專案 GitLab Issue
            sync_master_report_to_gitlab(report_file, ai_analysis, project_id=proj_id)

            # (F) 智慧 Email 告警推播 (具備專案開關、舊疾自動靜音與惡化警報)
            email_enabled = proj_conf.get("email_alert_enabled", True) if proj_conf else True
            if not email_enabled:
                print(f"🔕 [Email 專案靜音] 專案 [{sname}] 已由管理員設定關閉 Email 告警推播。")
            else:
                has_prod = any("prod" in str(l.get("environment") or "").lower() for l in logs)
                should_send, reason = issue_tracker.should_send_email_alert(sname, lifecycle_summary, has_prod_error=has_prod)
                if should_send:
                    print(f"📧 [Email 觸發告警] {reason}，發送通知郵件至負責人...")
                    report_web_url = f"{CHATOPS_BASE_URL}/report/{sname}/{os.path.basename(report_file)}"
                    send_alert_notification(sname, ai_analysis, report_url=report_web_url, aggregated_errors=aggregated)
                else:
                    print(f"🔕 [Email 智慧降噪] {reason}")

        # 3. 為其他本次無錯誤的專案同步更新活體總結 summary.md (確保每個專案都有總結)
        all_proj_names = set(projects_cfg.keys())
        if os.path.exists(reports_base_dir):
            for d in os.listdir(reports_base_dir):
                if os.path.isdir(os.path.join(reports_base_dir, d)) and not d.startswith("."):
                    all_proj_names.add(d)

        for sname in all_proj_names:
            if sname not in service_groups:
                update_project_summary(
                    service_name=sname,
                    aggregated_errors=[],
                    ai_analysis={"severity": "Normal", "correlation_analysis": "過去時間內無新增異常日誌，服務健康運行。"},
                    base_dir=reports_base_dir
                )

        # 4. 更新全域多專案總覽看板 index.md
        index_path = update_reports_index_dashboard(base_dir=reports_base_dir)
        print("\n" + "="*70)
        print("🎉 全域多專案分析與工單同步完成！")
        print(f"📊 SRE Sentinel 健康看板：file:///{index_path.replace(os.sep, '/')}")
        print("="*70 + "\n")
        
    else:
        print("🎉 過去時間內無任何錯誤日誌，系統健康狀態良好！")
        projects_cfg = load_projects_config()
        all_proj_names = set(projects_cfg.keys())
        if os.path.exists(reports_base_dir):
            for d in os.listdir(reports_base_dir):
                if os.path.isdir(os.path.join(reports_base_dir, d)) and not d.startswith("."):
                    all_proj_names.add(d)

        for sname in all_proj_names:
            update_project_summary(
                service_name=sname,
                aggregated_errors=[],
                ai_analysis={"severity": "Normal", "correlation_analysis": "過去時間內無新增異常日誌，服務健康運行。"},
                base_dir=reports_base_dir
            )
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
