from __future__ import annotations

import os
import re
import json
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional

def sanitize_filename(name: str) -> str:
    """去除檔名不合法字元"""
    return re.sub(r'[\/:*?"<>| ]+', '_', name).strip('_')

def save_or_update_master_report(
    aggregated_errors: List[Dict[str, Any]],
    ai_global_analysis: Dict[str, Any],
    service_name: str = "global",
    base_dir: str = "reports"
) -> str:
    """
    產出/更新特定微服務專案的全景 SRE 診斷報告：
    reports/<service_name>/report_YYYYMMDD.md
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    now_formatted = now.strftime("%Y-%m-%d %H:%M:%S")
    
    project_dir = os.path.join(base_dir, service_name)
    os.makedirs(project_dir, exist_ok=True)
    report_file = os.path.join(project_dir, f"report_{date_str}.md")
    
    title = ai_global_analysis.get("title", f"🏥 [SRE 全景診斷] {service_name} 異常分析與協同修復報告 ({date_str})")
    severity = ai_global_analysis.get("severity", "High")
    correlation_analysis = ai_global_analysis.get("correlation_analysis", "系統多項異常之全域關聯與因果鏈分析。")
    coordinated_fix_plan = ai_global_analysis.get("coordinated_fix_plan", "跨檔案協同修復架構與代碼修補說明。")
    sub_items = ai_global_analysis.get("sub_items", [])
    
    total_distinct_issues = len(aggregated_errors)
    total_occurrences = sum(e.get("count", 1) for e in aggregated_errors)
    
    # 建立各問題細節區塊
    issues_breakdown_lines = []
    for idx, item in enumerate(aggregated_errors, start=1):
        loc = item.get("error_location", "unknown:0")
        etype = item.get("error_type", "UnknownError")
        count = item.get("count", 1)
        role = item.get("role", "N/A")
        emsg = item.get("error_message", "N/A")
        stack = item.get("error_stack_sample", "") or "無詳細堆疊"
        code_context = item.get("code_context", "")
        
        is_new = item.get("is_new", False)
        is_ongoing = item.get("is_ongoing", False)
        consecutive_days = item.get("consecutive_days", 1)
        tot_history = item.get("total_occurrences", count)
        
        if is_ongoing and consecutive_days > 1:
            lifecycle_badge = f"【⏳ 連續第 {consecutive_days} 天未修 ｜ 歷史累計 {tot_history} 次】"
        elif is_new:
            lifecycle_badge = "【🆕 今日首次發現】"
        else:
            lifecycle_badge = ""
        
        code_section = f"""* **真實代碼切片 (main 分支)**：
```python
{code_context}
```
""" if code_context else ""

        issues_breakdown_lines.append(f"""### 📌 問題 {idx}：`{etype}` at `{loc}` {lifecycle_badge}
* **微服務專案**：`{service_name}`
* **本日發生次數**：**{count} 次** (歷史累計: {tot_history} 次)
* **影響業務情境**：`{role}`
* **錯誤訊息**：
```text
{emsg}
```
{code_section}* **堆疊追蹤片段**：
```python
{stack[:600]}
```
""")
    
    issues_breakdown_md = "\n".join(issues_breakdown_lines)
    sub_items_md = "\n".join(f"- [ ] {item}" for item in sub_items) if sub_items else "- [ ] 執行協同修補並通過全量 pytest 測試"

    master_md = f"""# {title}

> **報告產生時間**：{now_formatted}  
> **整體嚴重度**：**{severity}** ｜ **獨立問題數**：`{total_distinct_issues}` 個 ｜ **累積異常日誌**：`{total_occurrences}` 次

---

## 🌐 1. 全域連鎖因果與相互影響分析 (Correlation & Root Cause)

{correlation_analysis}

---

## 🛠️ 2. 跨檔案協同修復計畫 (Coordinated Multi-File Fix Plan)

{coordinated_fix_plan}

### 📋 協同修復與驗證清單 (Checklist)
{sub_items_md}

---

## 🔍 3. 當日所有問題詳細剖析 (Issues Breakdown)

{issues_breakdown_md}

---
*本報告由 SRE AI Agent 全域分析大腦自動彙整產出，支援多問題原子性協同自癒。*
"""

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(master_md)
        
    update_reports_index_dashboard(base_dir)
    return report_file

def update_reports_index_dashboard(base_dir: str = "reports") -> str:
    """
    掃描 reports/ 下所有專案子目錄，生成全域總覽儀表板 (reports/index.md)
    與各微服務專屬 (reports/<service_name>/index.md)。
    """
    if not os.path.exists(base_dir):
        os.makedirs(base_dir, exist_ok=True)
        
    index_file = os.path.join(base_dir, "index.md")
    project_summaries = []
    
    for item in os.listdir(base_dir):
        sub_path = os.path.join(base_dir, item)
        if not os.path.isdir(sub_path) or item.startswith("."):
            continue
            
        service_name = item
        project_reports = []
        for file in os.listdir(sub_path):
            if file.startswith("report_") and file.endswith(".md"):
                fpath = os.path.join(sub_path, file)
                try:
                    with open(fpath, "r", encoding="utf-8") as rf:
                        txt = rf.read()
                    sev_m = re.search(r"\*\*整體嚴重度\*\*：\*\*(.*?)\*\*", txt)
                    sev = sev_m.group(1).strip() if sev_m else "High"
                    time_m = re.search(r"\*\*報告產生時間\*\*：(.*?)\s+", txt)
                    gtime = time_m.group(1).strip() if time_m else "-"
                    issues_m = re.search(r"\*\*獨立問題數\*\*：`(\d+)` 個", txt)
                    icnt = issues_m.group(1) if issues_m else "0"
                    project_reports.append({
                        "file": file,
                        "path": f"{service_name}/{file}",
                        "severity": sev,
                        "time": gtime,
                        "issues": icnt
                    })
                except Exception:
                    pass
                    
        project_reports.sort(key=lambda x: x["file"], reverse=True)
        
        # 產生該專案子目錄內部的 index.md
        proj_index_path = os.path.join(sub_path, "index.md")
        proj_rows = [f"| [{pr['file']}]({pr['file']}) | {pr['severity']} | `{pr['issues']}` 個問題 | {pr['time']} |" for pr in project_reports]
        proj_rows_str = "\n".join(proj_rows) if proj_rows else "| 尚無歷史診斷報告 | - | 0 | - |"
        with open(proj_index_path, "w", encoding="utf-8") as pf:
            pf.write(f"""# 📂 {service_name} 歷史診斷報表索引

| 報告檔名 | 嚴重度 | 問題數 | 分析時間 |
| :--- | :---: | :---: | :--- |
{proj_rows_str}
""")

        latest = project_reports[0] if project_reports else None
        project_summaries.append({
            "service_name": service_name,
            "total_reports": len(project_reports),
            "latest": latest
        })
        
    summary_rows = []
    for ps in project_summaries:
        sname = ps["service_name"]
        lat = ps["latest"]
        if lat:
            icon = "🔴" if lat["severity"] == "High" else ("🟡" if lat["severity"] == "Medium" else "🟢")
            summary_rows.append(
                f"| **{sname}** | {icon} {lat['severity']} | `{lat['issues']}` 個 | {lat['time']} | [{lat['file']}]({lat['path']}) ｜ [專案歷史]({sname}/index.md) |"
            )
        else:
            summary_rows.append(f"| **{sname}** | 🟢 正常 | 0 個 | - | [查看專案目錄]({sname}/index.md) |")
            
    summary_table = "\n".join(summary_rows) if summary_rows else "| 目前尚未監控任何微服務專案 | - | - | - | - |"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    global_index_md = f"""# 🏥 SRE Sentinel 全域多專案微服務監控儀表板

> **總控看板更新時間**：{now_str}  
> **受監控微服務數量**：`{len(project_summaries)}` 個專案

---

## 📊 微服務專案健康總覽矩陣 (Service Health Matrix)

| 微服務專案名稱 | 最新狀態 | 今日異常數 | 最近診斷時間 | 最新報表與歷史連結 |
| :--- | :---: | :---: | :---: | :--- |
{summary_table}

---
*點擊各微服務專案之報表連結，可查看各專案之程式碼上下文切片、根因分析與一鍵自癒狀態。*
"""

    with open(index_file, "w", encoding="utf-8") as f:
        f.write(global_index_md)
        
    return index_file


def get_current_week_str(dt: Optional[datetime] = None) -> str:
    """取得標準週次字串，例如 2026_W42"""
    target = dt or datetime.now()
    iso = target.isocalendar()
    return f"{iso[0]}_W{iso[1]:02d}"


def save_project_weekly_report(
    service_name: str,
    stats: Dict[str, Any],
    ai_analysis: Dict[str, Any],
    week_str: Optional[str] = None,
    base_dir: str = "reports"
) -> str:
    """
    產出/更新特定微服務專案的專屬每週 SRE 綜整報告：
    reports/<service_name>/weekly_YYYY_Www.md
    """
    now = datetime.now()
    w_str = week_str or get_current_week_str(now)
    now_formatted = now.strftime("%Y-%m-%d %H:%M:%S")

    project_dir = os.path.join(base_dir, service_name)
    os.makedirs(project_dir, exist_ok=True)
    report_file = os.path.join(project_dir, f"weekly_{w_str}.md")

    title = ai_analysis.get("title", f"📊 [SRE 週度營運報表] {service_name} 全景健康綜整 ({w_str})")
    severity = ai_analysis.get("severity", "Normal")
    trend_summary = ai_analysis.get("trend_summary", "過去 7 天系統運行總結。")
    recurring_issues = ai_analysis.get("recurring_issues", [])
    roadmap_items = ai_analysis.get("action_items", [])

    total_logs_7d = stats.get("total_errors", 0)
    distinct_issues = stats.get("distinct_issues_count", 0)
    daily_breakdown = stats.get("daily_breakdown", {})
    env_breakdown = stats.get("env_breakdown", {})

    # 每日趨勢表格
    daily_rows = []
    for day, cnt in sorted(daily_breakdown.items()):
        daily_rows.append(f"| `{day}` | **{cnt}** 次 |")
    daily_table = "\n".join(daily_rows) if daily_rows else "| 過去 7 天 | 無異常記錄 |"

    # 環境分佈列表
    env_lines = [f"- **{k}**: {v} 次錯誤" for k, v in env_breakdown.items()]
    env_text = "\n".join(env_lines) if env_lines else "- 過去一週無環境異常分佈記錄"

    # 重複頑疾區塊
    recurring_lines = []
    if isinstance(recurring_issues, list) and recurring_issues:
        for idx, issue in enumerate(recurring_issues, 1):
            if isinstance(issue, dict):
                loc = issue.get("location", "未知位置")
                desc = issue.get("description", "")
                imp = issue.get("impact", "")
                recurring_lines.append(f"### ⚠️ 頑疾 {idx}：`{loc}`\n* **問題描述**：{desc}\n* **潛在風險與衝擊**：{imp}\n")
            else:
                recurring_lines.append(f"- **問題 {idx}**：{str(issue)}")
    recurring_text = "\n".join(recurring_lines) if recurring_lines else "🎉 本週未檢測到長期重複發生的重大頑疾，服務健康良好！"

    # 優化行動清單
    action_lines = []
    if isinstance(roadmap_items, list) and roadmap_items:
        for it in roadmap_items:
            action_lines.append(f"- [ ] {it}")
    action_text = "\n".join(action_lines) if action_lines else "- [ ] 持續關注日常告警，維持程式庫依賴更新"

    md_content = f"""# {title}

> **微服務專案**：`{service_name}`  
> **統計週期**：過去 7 天（168 小時） ｜ **週次標記**：`{w_str}`  
> **整體穩定度評級**：**{severity}** ｜ **7 天累計異常數**：`{total_logs_7d}` 次 ｜ **獨立問題項目**：`{distinct_issues}` 個  
> **報告生成時間**：{now_formatted}

---

## 📈 1. 本週穩定度趨勢與總結 (Weekly Executive Summary)

{trend_summary}

### 📅 過去 7 天每日異常走勢
| 日期 | 錯誤發生次數 |
| :--- | :---: |
{daily_table}

### 🌐 運行環境分佈 (Environments)
{env_text}

---

## 🦠 2. 本週重複性頑疾與技術債追蹤 (Chronic & Recurring Issues)

{recurring_text}

---

## 🛠️ 3. 下週專案架構調優建議與行動清單 (Actionable SRE Roadmap)

### 📋 建議落實清單 (Checklist)
{action_text}

---
*本週報由 SRE AI Sentinel 智慧分析大腦自動彙整產出，專門供 `{service_name}` 專案團隊維護系統穩定度使用。*
"""

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(md_content)

    return report_file


def save_global_weekly_report(
    all_stats: Dict[str, Any],
    ai_global_analysis: Dict[str, Any],
    week_str: Optional[str] = None,
    base_dir: str = "reports"
) -> str:
    """
    產出/更新全系統跨微服務的全域總控週報：
    reports/weekly/weekly_YYYY_Www.md
    """
    now = datetime.now()
    w_str = week_str or get_current_week_str(now)
    now_formatted = now.strftime("%Y-%m-%d %H:%M:%S")

    weekly_dir = os.path.join(base_dir, "weekly")
    os.makedirs(weekly_dir, exist_ok=True)
    report_file = os.path.join(weekly_dir, f"weekly_{w_str}.md")

    title = ai_global_analysis.get("title", f"🌐 [SRE 全域總體週報] 全微服務系統營運與架構健康評估 ({w_str})")
    global_severity = ai_global_analysis.get("severity", "Normal")
    executive_summary = ai_global_analysis.get("executive_summary", "全系統一週營運綜整。")
    cascading_analysis = ai_global_analysis.get("cascading_analysis", "跨微服務連鎖相依性與架構評估。")
    strategic_recommendations = ai_global_analysis.get("strategic_recommendations", [])
    
    total_services = all_stats.get("total_services", 0)
    total_system_errors = all_stats.get("total_system_errors", 0)
    service_rankings = all_stats.get("service_rankings", [])

    ranking_rows = []
    for r in service_rankings:
        sname = r.get("service_name", "unknown")
        cnt = r.get("errors_7d", 0)
        sev = r.get("severity", "Normal")
        env_str = r.get("top_env", "production")
        ranking_rows.append(f"| **{sname}** | {sev} | **{cnt}** 次 | `{env_str}` |")
    ranking_table = "\n".join(ranking_rows) if ranking_rows else "| 全微服務系統運作良好 | Normal | 0 | - |"

    strat_lines = []
    if isinstance(strategic_recommendations, list) and strategic_recommendations:
        for item in strategic_recommendations:
            strat_lines.append(f"- [ ] {item}")
    strat_text = "\n".join(strat_lines) if strat_lines else "- [ ] 持續監控全域連線池與微服務調用超時設定"

    md_content = f"""# {title}

> **統計範圍**：全域受監控微服務（共 `{total_services}` 個專案）  
> **統計週期**：過去 7 天（168 小時） ｜ **週次標記**：`{w_str}`  
> **全系統健康評級**：**{global_severity}** ｜ **系統累計異常總數**：`{total_system_errors}` 次  
> **報告生成時間**：{now_formatted}

---

## 🧭 1. 全系統營運穩定度總結 (Executive Overview)

{executive_summary}

---

## 🏆 2. 本週微服務專案故障排行矩陣 (Service Reliability Rankings)

| 微服務專案 | 健康評級 | 7 天累計錯誤 | 主要受影響環境 |
| :--- | :---: | :---: | :--- |
{ranking_table}

---

## 🔗 3. 跨微服務連鎖因果與系統相依性分析 (Cross-Service Cascading Analysis)

{cascading_analysis}

---

## 🏛️ 4. 全域 SRE 架構調優與技術債清理指南 (Strategic Roadmap)

### 📋 全域優化建議清單
{strat_text}

---
*本全域週報由 SRE AI Sentinel 自動分析大腦彙整產出，專供 SRE 架構師與技術主管綜觀全系統穩定度使用。*
"""

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(md_content)

    return report_file


def list_all_weekly_reports(base_dir: str = "reports") -> Dict[str, Any]:
    """
    掃描 reports/ 下的所有週報檔案，回傳結構化的週報索引：
    {
        "weeks": ["2026_W42", "2026_W41", ...],
        "global": [{"week": "2026_W42", "file": "weekly_2026_W42.md", "title": "...", "severity": "...", "time": "..."}],
        "projects": {
            "service_a": [{"week": "2026_W42", "file": "weekly_2026_W42.md", ...}]
        }
    }
    """
    if not os.path.exists(base_dir):
        return {"weeks": [], "global": [], "projects": {}}

    all_weeks_set = set()
    global_reports = []
    projects_reports: Dict[str, List[Dict[str, Any]]] = {}

    def parse_weekly_header(fpath: str) -> Dict[str, str]:
        info = {"title": "", "severity": "Normal", "time": "-", "issues": "0"}
        try:
            with open(fpath, "r", encoding="utf-8") as rf:
                head = rf.read(1500)
            t_m = re.search(r"^#\s+(.+)$", head, re.MULTILINE)
            if t_m:
                info["title"] = t_m.group(1).strip()
            s_m = re.search(r"\*\*(?:整體穩定度評級|全系統健康評級)\*\*：\*\*(.*?)\*\*", head)
            if s_m:
                info["severity"] = s_m.group(1).strip()
            tm_m = re.search(r"\*\*報告生成時間\*\*：(.*?)$", head, re.MULTILINE)
            if tm_m:
                info["time"] = tm_m.group(1).strip()
            iss_m = re.search(r"\*\*(?:7 天累計異常數|系統累計異常總數)\*\*：`(\d+)`", head)
            if iss_m:
                info["issues"] = iss_m.group(1).strip()
        except Exception:
            pass
        return info

    # 1. 掃描全域週報 (reports/weekly/)
    weekly_dir = os.path.join(base_dir, "weekly")
    if os.path.exists(weekly_dir):
        for f in os.listdir(weekly_dir):
            if f.startswith("weekly_") and f.endswith(".md"):
                w = f[len("weekly_"):-len(".md")]
                all_weeks_set.add(w)
                meta = parse_weekly_header(os.path.join(weekly_dir, f))
                global_reports.append({
                    "week": w,
                    "file": f,
                    "title": meta["title"] or f"全域總控週報 ({w})",
                    "severity": meta["severity"],
                    "time": meta["time"],
                    "issues": meta["issues"]
                })
        global_reports.sort(key=lambda x: x["week"], reverse=True)

    # 2. 掃描各微服務專案週報 (reports/<service_name>/weekly_*.md)
    for item in os.listdir(base_dir):
        sub = os.path.join(base_dir, item)
        if not os.path.isdir(sub) or item in ("weekly", ".git"):
            continue
        sname = item
        proj_reps = []
        for f in os.listdir(sub):
            if f.startswith("weekly_") and f.endswith(".md"):
                w = f[len("weekly_"):-len(".md")]
                all_weeks_set.add(w)
                meta = parse_weekly_header(os.path.join(sub, f))
                proj_reps.append({
                    "week": w,
                    "file": f,
                    "title": meta["title"] or f"{sname} 週報 ({w})",
                    "severity": meta["severity"],
                    "time": meta["time"],
                    "issues": meta["issues"]
                })
        proj_reps.sort(key=lambda x: x["week"], reverse=True)
        if proj_reps:
            projects_reports[sname] = proj_reps

    sorted_weeks = sorted(list(all_weeks_set), reverse=True)
    return {
        "weeks": sorted_weeks,
        "global": global_reports,
        "projects": projects_reports
    }

