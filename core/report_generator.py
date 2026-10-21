from __future__ import annotations

import os
import re
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from core.issue_tracker import IssueLifecycleTracker

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
    
    raw_title = ai_global_analysis.get("title", "")
    if not raw_title or "全景診斷" in raw_title or "[SRE" in raw_title:
        title = f"📝 {service_name} 每日異常診斷 ({now.strftime('%Y-%m-%d')})"
    else:
        title = raw_title
        
    severity = ai_global_analysis.get("severity", "High")
    correlation_analysis = ai_global_analysis.get("correlation_analysis", "系統多項異常之因果鏈與原因分析。")
    coordinated_fix_plan = ai_global_analysis.get("coordinated_fix_plan", "代碼修補說明與修改建議。")
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
        
        from core.issue_tracker import compute_fingerprint
        fp = item.get("fingerprint") or compute_fingerprint(service_name, loc, etype)
        is_ack = item.get("is_acknowledged", False) or item.get("status") == "acknowledged"

        if is_ack:
            lifecycle_badge = f"【👁️ 已知待排期（免發信） ｜ 歷史累計 {tot_history} 次】"
            action_btn = f"""<div style="margin: 8px 0 12px 0;"><button onclick="unacknowledgeIssue('{service_name}', '{fp}')" style="display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; font-size: 11px; font-weight: 500; color: #4f46e5; background-color: #eef2ff; border: 1px solid #c7d2fe; border-radius: 4px; cursor: pointer;">↩️ 恢復告警追蹤</button></div>"""
        elif is_ongoing and consecutive_days > 1:
            lifecycle_badge = f"【⏳ 連續第 {consecutive_days} 天未修 ｜ 歷史累計 {tot_history} 次】"
            action_btn = f"""<div style="margin: 8px 0 12px 0;"><button onclick="acknowledgeIssue('{service_name}', '{fp}')" style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; font-size: 11px; font-weight: 600; color: #475569; background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer;">🔕 標記為已知 / 暫不處理</button></div>"""
        elif is_new:
            lifecycle_badge = "【🆕 今日首次發現】"
            action_btn = f"""<div style="margin: 8px 0 12px 0;"><button onclick="acknowledgeIssue('{service_name}', '{fp}')" style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; font-size: 11px; font-weight: 600; color: #475569; background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer;">🔕 標記為已知 / 暫不處理</button></div>"""
        else:
            lifecycle_badge = ""
            action_btn = f"""<div style="margin: 8px 0 12px 0;"><button onclick="acknowledgeIssue('{service_name}', '{fp}')" style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; font-size: 11px; font-weight: 600; color: #475569; background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer;">🔕 標記為已知 / 暫不處理</button></div>"""
        
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
{action_btn}
""")
    
    issues_breakdown_md = "\n".join(issues_breakdown_lines)
    sub_items_md = "\n".join(f"- [ ] {item}" for item in sub_items) if sub_items else "- [ ] 執行修補並通過全量測試"

    master_md = f"""# {title}

> **報告產生時間**：{now_formatted}  
> **整體健康狀態**：**{severity}** ｜ **獨立問題數**：`{total_distinct_issues}` 個 ｜ **累積異常日誌**：`{total_occurrences}` 次

---

## 🔍 1. 今日問題分析 (Root Cause)

{correlation_analysis}

---

## 🛠️ 2. 建議修復方式 (Fix Plan)

{coordinated_fix_plan}

### 📋 修復與驗證待辦 (Checklist)
{sub_items_md}

---

## 💻 3. 今日異常清單與出錯代碼 (Issues Breakdown)

{issues_breakdown_md}

---
*本報告由 SRE AI Agent 自動巡檢產出。*
"""

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(master_md)
        
    # 雙軌同步寫入資料庫 Diagnosis 表
    try:
        from core.database import get_db_session, Diagnosis
        session = get_db_session()
        diag = session.query(Diagnosis).filter_by(service_name=service_name, report_date=now.strftime("%Y-%m-%d")).first()
        if not diag:
            diag = Diagnosis(service_name=service_name, report_date=now.strftime("%Y-%m-%d"))
            session.add(diag)
        diag.severity = severity
        diag.title = title
        diag.root_cause_summary = correlation_analysis
        diag.coordinated_fix_plan = coordinated_fix_plan
        diag.sub_items_json = json.dumps(sub_items, ensure_ascii=False) if sub_items else "[]"
        session.commit()
        session.close()
    except Exception as dbe:
        print(f"⚠️ [ReportGenerator] DB Diagnosis save failed: {dbe}")

    update_reports_index_dashboard(base_dir)
    return report_file

def update_reports_index_dashboard(base_dir: str = "reports") -> str:
    """
    掃描 reports/ 下所有專案子目錄，生成全域總覽儀表板 (reports/index.md)
    與各微服務專屬 (reports/<service_name>/index.md)。
    排除 weekly 等非微服務目錄。
    """
    if not os.path.exists(base_dir):
        os.makedirs(base_dir, exist_ok=True)
        
    index_file = os.path.join(base_dir, "index.md")
    project_summaries = []
    
    for item in os.listdir(base_dir):
        sub_path = os.path.join(base_dir, item)
        if not os.path.isdir(sub_path) or item.startswith(".") or item == "weekly":
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

> 提示：專案最新全貌與 7 天趨勢，請查看 [活體總表 summary.md](summary.md)

| 歷史報告存根 | 嚴重度 | 問題數 | 分析時間 |
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
        has_summary = os.path.exists(os.path.join(base_dir, sname, "summary.md"))
        summary_link = f"[{sname} 活體總表]({sname}/summary.md)" if has_summary else f"[{sname}/summary.md]({sname}/summary.md)"
        if lat:
            icon = "🔴" if lat["severity"] == "High" else ("🟡" if lat["severity"] == "Medium" else "🟢")
            summary_rows.append(
                f"| **{sname}** | {icon} {lat['severity']} | `{lat['issues']}` 個 | {lat['time']} | {summary_link} ｜ [歷史存根]({sname}/index.md) |"
            )
        else:
            summary_rows.append(f"| **{sname}** | 🟢 正常 | 0 個 | - | {summary_link} ｜ [歷史存根]({sname}/index.md) |")
            
    summary_table = "\n".join(summary_rows) if summary_rows else "| 目前尚未監控任何微服務專案 | - | - | - | - |"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    global_index_md = f"""# 🏥 SRE Sentinel 全域多專案微服務監控儀表板

> **總控看板更新時間**：{now_str}  
> **受監控微服務數量**：`{len(project_summaries)}` 個專案

---

## 📊 微服務專案健康總覽矩陣 (Service Health Matrix)

| 微服務專案名稱 | 最新狀態 | 今日異常數 | 最近診斷時間 | 活體總表與歷史存根 |
| :--- | :---: | :---: | :---: | :--- |
{summary_table}

---
*點擊「活體總表」可檢視該微服務過去 7 天演變矩陣與 AI 沉澱根因。*
"""

    with open(index_file, "w", encoding="utf-8") as f:
        f.write(global_index_md)
        
    return index_file


def update_project_summary(
    service_name: str,
    aggregated_errors: List[Dict[str, Any]],
    ai_analysis: Dict[str, Any],
    base_dir: str = "reports"
) -> str:
    """
    建立或就地更新專案活體總結報告：
    reports/<service_name>/summary.md
    
    1. 專案現況卡片 (Status Overview)
    2. 滾動 7 天按日切片趨勢矩陣 (7-Day Daily Trend)
    3. 活躍問題與根因字典 (Active Issues & Root Causes)
    4. 暫緩技術債專區 (Acknowledged Tech Debt)
    5. 已平息歷史事件簿 (Resolved Archive)
    """
    from datetime import datetime, timedelta
    
    project_dir = os.path.join(base_dir, service_name)
    os.makedirs(project_dir, exist_ok=True)
    summary_file = os.path.join(project_dir, "summary.md")
    
    now = datetime.now()
    now_formatted = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    
    # 讀取狀態檔 (issue_state.json) 並自動執行指紋正規化遷移
    tracker = IssueLifecycleTracker(base_dir=base_dir)
    saved_state = tracker.load_state(service_name)
    issues_dict = saved_state.get("issues", {})

    # 1. 產生過去 7 天的日期序列 (D-6 ~ Today)
    date_objs = [now - timedelta(days=i) for i in range(6, -1, -1)]
    date_strs = [d.strftime("%Y-%m-%d") for d in date_objs]
    col_headers = [d.strftime("%m/%d") for d in date_objs]
    col_headers[-1] += " (今日)"

    # 2. 為每個問題統計 7 天每日分佈
    current_map = {e.get("fingerprint"): e for e in aggregated_errors if e.get("fingerprint")}
    all_fps = list(dict.fromkeys(list(current_map.keys()) + list(issues_dict.keys())))

    trend_rows = []
    active_issues = []
    acknowledged_issues = []
    resolved_issues = []

    total_7d_system = 0
    total_today = sum(e.get("count", 1) for e in aggregated_errors)

    for fp in all_fps:
        cur_err = current_map.get(fp)
        saved_item = issues_dict.get(fp, {})

        loc = cur_err.get("error_location") if cur_err else saved_item.get("error_location", "unknown:0")
        etype = cur_err.get("error_type") if cur_err else saved_item.get("error_type", "UnknownError")
        first_seen = saved_item.get("first_seen_date", today_str)
        consecutive_d = saved_item.get("consecutive_days", 1)
        tot_hist = saved_item.get("total_occurrences", cur_err.get("count", 1) if cur_err else 0)
        status = saved_item.get("status", "new")
        if cur_err and cur_err.get("status"):
            status = cur_err.get("status")
        is_ack = (status == "acknowledged") or saved_item.get("is_acknowledged", False)
        ack_note = saved_item.get("acknowledge_note", "")

        # 每日歷史次數
        daily_hist = saved_item.get("daily_history", {})
        counts_7d = []
        for d in date_strs:
            if d == today_str and cur_err:
                cnt = cur_err.get("count", 0)
            else:
                cnt = daily_hist.get(d, 0)
            counts_7d.append(cnt)

        sum_7d = sum(counts_7d)
        total_7d_system += sum_7d

        # 趨勢判定
        today_cnt = counts_7d[-1]
        yest_cnt = counts_7d[-2] if len(counts_7d) >= 2 else 0

        if is_ack:
            trend_label = "🔕 已知待排期"
        elif today_cnt == 0 and sum(counts_7d[-3:]) == 0:
            trend_label = "🟢 已平息"
        elif today_cnt >= 2 * max(yest_cnt, 1) and today_cnt >= 5:
            trend_label = "📈 快速飆升"
        elif consecutive_d > 1:
            trend_label = f"⏳ 連續第 {consecutive_d} 天"
        else:
            trend_label = "🆕 今日新發"

        # 產出表格行：若近 7 天累計 > 0、今日有發生、或為標記待排期之技術債，才列入滾動趨勢矩陣
        if sum_7d > 0 or today_cnt > 0 or is_ack:
            counts_cells = " | ".join(f"**{c}**" if c > 0 else "0" for c in counts_7d)
            trend_rows.append(f"| `{etype[:38]}` | {counts_cells} | **{sum_7d}** 次 | {trend_label} |")

        # 分類詳細資訊
        cached_info = saved_item.get("cached_analysis") or {}
        raw_root_cause = cached_info.get("root_cause_summary") or ai_analysis.get("correlation_analysis") or "待進一步排查底層連線與業務邏輯。"
        clean_root_cause = re.sub(r'^(【⏳\s*連續頑疾持續追蹤中.*?】\s*)+', '', raw_root_cause, flags=re.MULTILINE).strip()
        root_cause = (f"【⏳ 連續頑疾持續追蹤中（已連續 {consecutive_d} 天）】\n" + clean_root_cause) if consecutive_d > 1 else clean_root_cause

        item_detail = {
            "fp": fp,
            "etype": etype,
            "loc": loc,
            "first_seen": first_seen,
            "consecutive_d": consecutive_d,
            "today_count": today_cnt,
            "total_occurrences": tot_hist,
            "root_cause": root_cause,
            "ack_note": ack_note,
            "emsg": cur_err.get("error_message") if cur_err else saved_item.get("error_message", "N/A"),
            "code_context": cur_err.get("code_context", "") if cur_err else ""
        }

        if is_ack:
            acknowledged_issues.append(item_detail)
        elif today_cnt > 0 or status in ("new", "ongoing"):
            active_issues.append(item_detail)
        elif sum_7d > 0:
            resolved_issues.append(item_detail)

    # 組合表格標頭
    table_header = f"| 異常問題項目 | " + " | ".join(col_headers) + " | 7天累計 | 趨勢判定 |\n"
    table_divider = "| :--- | " + " | ".join([":---:"] * len(col_headers)) + " | :---: | :---: |\n"
    trend_table_md = table_header + table_divider + ("\n".join(trend_rows) if trend_rows else "| 目前無任何異常日誌記錄 | - | - | - | - | - | - | - | - | 🟢 系統健康 |\n")

    # 健康度等級
    sev = ai_analysis.get("severity", "Normal")
    if total_today > 50 or any("飆升" in r for r in trend_rows):
        sev = "Critical"
    elif total_today > 0:
        sev = "Warning" if sev != "Critical" else "Critical"

    # 組裝活躍問題區塊
    active_lines = []
    for idx, it in enumerate(active_issues, start=1):
        code_sec = f"\n* **真實代碼切片**：\n```python\n{it['code_context']}\n```" if it['code_context'] else ""
        btn_sec = f"""\n<div style="margin: 8px 0 14px 0;">
    <button onclick="acknowledgeIssue('{service_name}', '{it['fp']}')" style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; font-size: 11px; font-weight: 600; color: #475569; background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer;">
        🔕 標記為已知 / 暫不處理
    </button>
</div>"""
        active_lines.append(f"""### 📌 問題 {idx}：`{it['etype']}` at `{it['loc']}`
* **狀態標記**：⏳ 連續第 {it['consecutive_d']} 天 ｜ 歷史累計: **{it['total_occurrences']}** 次 (今日: {it['today_count']} 次)
* **首次發現日**：`{it['first_seen']}`
* **錯誤樣例**：`{str(it['emsg'])[:150]}`
* **AI 沉澱真實根因**：{it['root_cause']}{code_sec}{btn_sec}
""")
    active_md = "\n".join(active_lines) if active_lines else "🎉 目前無任何活躍異常，所有已知問題均已修復或平息！\n"

    # 組裝已知技術債 (Acknowledged)
    ack_lines = []
    for idx, it in enumerate(acknowledged_issues, start=1):
        unack_btn = f"""<button onclick="unacknowledgeIssue('{service_name}', '{it['fp']}')" style="display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; font-size: 11px; font-weight: 500; color: #4f46e5; background-color: #eef2ff; border: 1px solid #c7d2fe; border-radius: 4px; cursor: pointer; margin-left: 8px;">↩️ 恢復告警追蹤</button>"""
        ack_lines.append(f"""* **[👁️ 已知待排期]** `{it['etype']}` (`{it['loc']}`) {unack_btn}
  - **累計發生**：{it['total_occurrences']} 次 ｜ 首次發現：{it['first_seen']}
  - **工程師備註**：`{it['ack_note']}`
  - **根本成因備忘**：{it['root_cause']}
""")
    ack_md = "\n".join(ack_lines) if ack_lines else "*目前尚無標記為暫緩之技術債。*\n"

    # 組裝已平息歸檔
    res_lines = []
    for it in resolved_issues:
        res_lines.append(f"- 🟢 `{it['etype']}` (`{it['loc']}`) — 歷史累計 {it['total_occurrences']} 次，近 3 天已無發生 (已自癒/已修復)")
    resolved_md = "\n".join(res_lines) if res_lines else "*暫無已歸檔之歷史平息項目。*\n"

    # 最終 Markdown
    summary_md = f"""# 📊 {service_name} 異常總覽與 7 天趨勢

> **專案名稱**：`{service_name}`  
> **最後更新時間**：{now_formatted}  
> **整體健康狀態**：**{sev}** ｜ **今日活躍問題**：`{len(active_issues)}` 個 ｜ **今日報錯量**：`{total_today}` 次 ｜ **近 7 天累計**：`{total_7d_system}` 次

---

## 📊 1. 過去 7 天異常走勢 (Rolling 7-Day Trend)

{trend_table_md}

---

## 🔍 2. 目前未解決問題與原因 (Active Issues)

{active_md}

---

## 🔕 3. 已標記暫緩處理 (Acknowledged Issues)
> *此清單為工程團隊已確認知情、評估暫緩處理之項目，已自動關閉 Email 告警。*

{ack_md}

---

## 🟢 4. 近期已修復 / 平息 (Resolved)

{resolved_md}

---
*本報告為 `{service_name}` 之單一總表，每次巡檢自動滾動更新。*
"""

    with open(summary_file, "w", encoding="utf-8") as f:
        f.write(summary_md)

    return summary_file

