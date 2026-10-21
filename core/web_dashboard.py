# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import re
import json
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_CONFIG_PATH = os.path.join(ROOT_DIR, "projects.json")

def load_projects_config() -> dict:
    if not os.path.exists(PROJECTS_CONFIG_PATH):
        return {}
    try:
        with open(PROJECTS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Dashboard] Failed to load projects.json: {e}")
        return {}

def get_services_env_stats_from_es(hours: int = 168) -> dict:
    """向 Elasticsearch 查詢各微服務在各個環境下的即時日誌與錯誤統計 (過去 7 天)"""
    try:
        from main import es_client, ES_INDEX
        q = {
            'size': 0,
            'query': {
                'range': {'@timestamp': {'gte': f'now-{hours}h', 'lte': 'now'}}
            },
            'aggs': {
                'by_service_dot': {
                    'terms': {'field': 'service.name.keyword', 'size': 50},
                    'aggs': {
                        'by_env': {
                            'terms': {'field': 'service.environment.keyword', 'size': 10},
                            'aggs': {
                                'errors': {
                                    'filter': {
                                        'bool': {'should': [{'term': {'level.keyword': 'ERROR'}}, {'term': {'log.level': 'error'}}]}
                                    }
                                }
                            }
                        }
                    }
                },
                'by_service_under': {
                    'terms': {'field': 'service_name.keyword', 'size': 50},
                    'aggs': {
                        'by_env': {
                            'terms': {'field': 'environment.keyword', 'size': 10},
                            'aggs': {
                                'errors': {
                                    'filter': {
                                        'bool': {'should': [{'term': {'level.keyword': 'ERROR'}}, {'term': {'log.level': 'error'}}]}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        res = es_client.search(index=ES_INDEX, body=q)
        results = {}
        for agg_name in ['by_service_dot', 'by_service_under']:
            for b in res.get('aggregations', {}).get(agg_name, {}).get('buckets', []):
                sname = b['key']
                if sname not in results:
                    results[sname] = {}
                for eb in b.get('by_env', {}).get('buckets', []):
                    ename = eb['key']
                    tot = eb['doc_count']
                    err = eb.get('errors', {}).get('doc_count', 0)
                    if ename not in results[sname]:
                        results[sname][ename] = {'total': tot, 'error': err}
                    else:
                        results[sname][ename]['total'] += tot
                        results[sname][ename]['error'] += err
        return results
    except Exception as e:
        print(f"⚠️ get_services_env_stats_from_es 失敗: {e}")
        return {}

def get_services_health(reports_base_dir: str = "reports", target_env: str = None) -> dict:
    base_dir = os.path.abspath(reports_base_dir)
    projects_cfg = load_projects_config()
    es_env_stats = get_services_env_stats_from_es(hours=168)
    
    services = {}
    
    # 1. 先從 projects.json 填入已知微服務
    for sname, conf in projects_cfg.items():
        services[sname] = {
            "service_name": sname,
            "description": conf.get("description", "微服務應用程式"),
            "project_id": conf.get("project_id"),
            "project_path": conf.get("project_path", ""),
            "branch": conf.get("branch", "main"),
            "status": "HEALTHY",
            "severity": "Normal",
            "issues_count": 0,
            "latest_time": "-",
            "latest_report_file": None,
            "total_reports": 0,
            "is_registered": True,
            "log_field": conf.get("log_field", ""),
            "log_value": conf.get("log_value", ""),
            "notification_emails": conf.get("notification_emails", ""),
            "email_alert_enabled": conf.get("email_alert_enabled", True),
            "history": [],
            "env_stats": es_env_stats.get(sname, {})
        }
        
    # 2. 掃描 reports 目錄 (排除 weekly, global 等非微服務的報表目錄)
    if os.path.exists(base_dir):
        for item in os.listdir(base_dir):
            sub_path = os.path.join(base_dir, item)
            if not os.path.isdir(sub_path) or item.startswith(".") or item in ("weekly", "global"):
                continue
            sname = item
            if sname not in services:
                services[sname] = {
                    "service_name": sname,
                    "description": "日誌自動偵測發現 (未註冊配置)",
                    "project_id": None,
                    "project_path": "",
                    "branch": "main",
                    "status": "HEALTHY",
                    "severity": "Normal",
                    "issues_count": 0,
                    "latest_time": "-",
                    "latest_report_file": None,
                    "total_reports": 0,
                    "is_registered": False,
                    "email_alert_enabled": True,
                    "history": [],
                    "env_stats": es_env_stats.get(sname, {})
                }
                
            report_files = []
            for file in os.listdir(sub_path):
                if file.startswith("report_") and file.endswith(".md"):
                    fpath = os.path.join(sub_path, file)
                    try:
                        with open(fpath, "r", encoding="utf-8") as rf:
                            txt = rf.read()
                        sev_m = re.search(r"\*\*整體嚴重度\*\*：\*\*(.*?)\*\*", txt)
                        sev = sev_m.group(1).strip() if sev_m else "High"
                        time_m = re.search(r"\*\*報告產生時間\*\*：(.*?)(?:\s|\n)", txt)
                        gtime = time_m.group(1).strip() if time_m else "-"
                        issues_m = re.search(r"\*\*獨立問題數\*\*：`(\d+)` 個", txt)
                        icnt = int(issues_m.group(1)) if issues_m else 0
                        
                        report_files.append({
                            "file": file,
                            "severity": sev,
                            "time": gtime,
                            "issues": icnt
                        })
                    except Exception:
                        pass
                        
            report_files.sort(key=lambda x: x["file"], reverse=True)
            services[sname]["total_reports"] = len(report_files)
            services[sname]["history"] = report_files
            
            today_str = datetime.now().strftime("%Y%m%d")
            today_date_str = datetime.now().strftime("%Y-%m-%d")

            if report_files:
                lat = report_files[0]
                services[sname]["latest_report_file"] = lat["file"]
                services[sname]["latest_time"] = lat["time"]
                
                # 判斷最新報告是否為「今天」產生
                # 報告檔名通常為 report_YYYYMMDD.md，或是時間字串以 YYYY-MM-DD 開頭
                is_today_report = (
                    f"report_{today_str}" in lat["file"] or 
                    (lat["time"] and lat["time"].startswith(today_date_str))
                )
                services[sname]["is_today_report"] = is_today_report

                if is_today_report:
                    services[sname]["issues_count"] = lat["issues"]
                    services[sname]["severity"] = lat["severity"]
                    if lat["severity"] == "High":
                        services[sname]["status"] = "CRITICAL"
                    elif lat["severity"] == "Medium":
                        services[sname]["status"] = "WARNING"
                    else:
                        services[sname]["status"] = "HEALTHY"
                else:
                    # 今天尚無報告或今天無新異常，今日問題數歸零，服務狀態為健康
                    services[sname]["issues_count"] = 0
                    services[sname]["severity"] = "Normal"
                    services[sname]["status"] = "HEALTHY"

            # 檢查是否存在活體總結報告 summary.md，若尚無則即時自動初始化
            summary_path = os.path.join(sub_path, "summary.md")
            if not os.path.exists(summary_path):
                try:
                    from core.report_generator import update_project_summary
                    update_project_summary(
                        service_name=sname,
                        aggregated_errors=[],
                        ai_analysis={"severity": "Normal", "correlation_analysis": "過去時間內無新增異常，服務運作良好。"},
                        base_dir=base_dir
                    )
                except Exception as _ex:
                    pass
            services[sname]["has_summary"] = os.path.exists(summary_path)

            # 讀取該微服務專屬之生命週期追蹤檔 (issue_state.json)
            state_file = os.path.join(sub_path, "issue_state.json")
            new_cnt = 0
            chronic_cnt = 0
            ack_cnt = 0
            max_days = 0
            new_list = []
            chronic_list = []
            ack_list = []
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as sf:
                        s_data = json.load(sf)
                    for fp, it in s_data.get("issues", {}).items():
                        issue_info = {
                            "fp": fp,
                            "error_type": str(it.get("error_type") or "UnknownError"),
                            "error_location": str(it.get("error_location") or "unknown:0"),
                            "today_count": it.get("today_count", 0),
                            "total_occurrences": it.get("total_occurrences", 0),
                            "consecutive_days": it.get("consecutive_days", 1),
                            "first_seen_date": it.get("first_seen_date", ""),
                            "last_seen_date": it.get("last_seen_date", ""),
                            "status": it.get("status", "new"),
                            "acknowledge_note": it.get("acknowledge_note", "")
                        }
                        if it.get("status") == "acknowledged" or it.get("is_acknowledged"):
                            ack_cnt += 1
                            ack_list.append(issue_info)
                        elif it.get("status") in ("new", "ongoing") and it.get("last_seen_date") == today_date_str:
                            c_days = it.get("consecutive_days", 1)
                            if c_days > 1:
                                chronic_cnt += 1
                                max_days = max(max_days, c_days)
                                chronic_list.append(issue_info)
                            else:
                                new_cnt += 1
                                new_list.append(issue_info)
                except Exception:
                    pass
            services[sname]["new_issues_count"] = new_cnt
            services[sname]["chronic_issues_count"] = chronic_cnt
            services[sname]["acknowledged_issues_count"] = ack_cnt
            services[sname]["max_consecutive_days"] = max_days
            services[sname]["issues_details"] = {
                "new": new_list,
                "chronic": chronic_list,
                "acknowledged": ack_list
            }

    # 3. 若有指定 target_env，依據該環境在 ES 的真實錯誤動態重算專案狀態
    if target_env and target_env.strip():
        t_env = target_env.strip().lower()
        for sname, s in services.items():
            # 支援大小寫模糊比對環境
            st = s.get("env_stats", {}).get(t_env, {"total": 0, "error": 0})
            if not st or (st.get("total", 0) == 0 and st.get("error", 0) == 0):
                for ek, ev in s.get("env_stats", {}).items():
                    if ek.lower() == t_env:
                        st = ev
                        break
            
            err_cnt = st.get("error", 0)
            tot_cnt = st.get("total", 0)
            s["target_env"] = t_env
            s["target_env_error"] = err_cnt
            s["target_env_total"] = tot_cnt
            
            # 正式環境 (production / formal)：零容忍！只要有任何錯誤直接視為 CRITICAL！
            is_prod = ("prod" in t_env or "formal" in t_env)
            
            if is_prod:
                if err_cnt > 0:
                    s["status"] = "CRITICAL"
                    s["severity"] = "High"
                    s["issues_count"] = s.get("issues_count") if (s.get("is_today_report") and s.get("issues_count", 0) > 0) else err_cnt
                else:
                    s["status"] = "HEALTHY"
                    s["severity"] = "Normal"
                    s["issues_count"] = 0
            else:
                # 測試 / 開發環境 (dev / local)
                if err_cnt >= 5:
                    s["status"] = "CRITICAL"
                    s["severity"] = "High"
                    s["issues_count"] = s.get("issues_count") if (s.get("is_today_report") and s.get("issues_count", 0) > 0) else err_cnt
                elif err_cnt > 0:
                    s["status"] = "WARNING"
                    s["severity"] = "Medium"
                    s["issues_count"] = s.get("issues_count") if (s.get("is_today_report") and s.get("issues_count", 0) > 0) else err_cnt
                else:
                    s["status"] = "HEALTHY"
                    s["severity"] = "Normal"
                    s["issues_count"] = 0

    total_services = len(services)
    critical_count = sum(1 for s in services.values() if s["status"] == "CRITICAL")
    warning_count = sum(1 for s in services.values() if s["status"] == "WARNING")
    total_issues = sum(s["issues_count"] for s in services.values())
    
    return {
        "total_services": total_services,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "healthy_count": total_services - critical_count - warning_count,
        "total_issues": total_issues,
        "services": list(services.values()),
        "target_env": target_env,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def render_dashboard_html(data: dict, gitlab_url: str = "http://192.168.20.112:8929", current_env: str = "") -> str:
    # 建立所有專案問題詳情 JSON，供前端 Modal 互動查詢
    issues_detail_map = {}
    for s in data["services"]:
        issues_detail_map[s["service_name"]] = s.get("issues_details", {"new": [], "chronic": [], "acknowledged": []})
    issues_detail_json = json.dumps(issues_detail_map, ensure_ascii=False)

    cards_html = ""
    for s in data["services"]:
        status = s["status"]
        sname_esc_js = s['service_name'].replace("'", "\\'")

        # 1. 狀態標籤與呼吸燈
        if status == "CRITICAL":
            badge_class = "bg-rose-50 text-rose-700 border-rose-200"
            dot_class = "bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.5)]"
            status_text = "CRITICAL ERROR"
        elif status == "WARNING":
            badge_class = "bg-amber-50 text-amber-700 border-amber-200"
            dot_class = "bg-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.5)]"
            status_text = "WARNING"
        else:
            badge_class = "bg-emerald-50 text-emerald-700 border-emerald-200"
            dot_class = "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
            status_text = "ALL HEALTHY"

        # 2. 編輯與刪除按鈕 (統一固定 28x28px 圖標按鈕，維持頂部槽位乾淨平整)
        btn_title = "自專案監控清單移除" if s["is_registered"] else "清理此專案殘留之歷史報告與快取"
        delete_btn = f'''<button onclick="deleteProject('{sname_esc_js}')" title="{btn_title}" class="w-7 h-7 flex items-center justify-center text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-lg transition text-xs">🗑️</button>'''

        desc_escaped = (s['description'] or "").replace("'", "\\'")
        proj_path_escaped = (s['project_path'] or "").replace("'", "\\'")
        log_f_escaped = (s.get("log_field") or "").replace("'", "\\'")
        log_v_escaped = (s.get("log_value") or "").replace("'", "\\'")
        emails_escaped = (s.get("notification_emails") or "").replace("'", "\\'")

        email_alert_enabled = s.get("email_alert_enabled", True)
        email_status_js = "true" if email_alert_enabled else "false"

        if s["is_registered"]:
            edit_btn = f'''<button onclick="openEditModal('{sname_esc_js}', '{desc_escaped}', '{proj_path_escaped}', '{s['project_id'] or ''}', '{s['branch']}', '{log_f_escaped}', '{log_v_escaped}', '{emails_escaped}', {email_status_js})" title="編輯配置" class="w-7 h-7 flex items-center justify-center text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 rounded-lg transition text-xs">✏️</button>'''
        else:
            edit_btn = f'''<button onclick="openEditModal('{sname_esc_js}', '', '', '', 'main', '', '', '', true)" title="此服務為 ELK 自動探測發現，點此補齊 GitLab 與監控配置" class="w-7 h-7 flex items-center justify-center text-indigo-600 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition border border-indigo-200 text-xs shadow-2xs font-bold">✏️</button>'''

        # 3. 日誌規則 Badge (未設定時顯示統一灰色邊框佔位符，嚴防高度凹陷)
        if s.get("log_field"):
            val_text = s.get("log_value") or s["service_name"]
            rule_badge = f'''
            <div class="mt-2 min-h-[26px] flex items-center">
                <span class="inline-flex items-center gap-1.5 max-w-full text-[11px] bg-slate-50 text-slate-600 px-2.5 py-0.5 rounded-md border border-slate-200 font-mono truncate" title="日誌識別規則: {s['log_field']} = {val_text}">
                    <span class="text-slate-400 font-sans font-semibold">日誌:</span>
                    <span class="font-bold text-slate-800 truncate">{s["log_field"]} = "{val_text}"</span>
                </span>
            </div>'''
        else:
            rule_badge = '''
            <div class="mt-2 min-h-[26px] flex items-center">
                <span class="inline-flex items-center gap-1.5 max-w-full text-[11px] text-slate-400 px-2.5 py-0.5 rounded-md border border-dashed border-slate-200 font-mono truncate" title="未指定特殊欄位，預設以 service_name 進行日誌比對">
                    <span class="font-sans font-medium">日誌:</span>
                    <span class="truncate">預設 service_name 欄位比對</span>
                </span>
            </div>'''

        # 3-2. Email 告警開關與通知收件人 Badge (支援一鍵切換啟用/靜音)
        if email_alert_enabled:
            toggle_pill = f'''<button type="button" onclick="toggleEmailAlert('{sname_esc_js}')" title="點擊將此專案之異常 Email 告警靜音" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-100 text-emerald-800 hover:bg-rose-100 hover:text-rose-700 border border-emerald-200 transition cursor-pointer flex-shrink-0"><span>🟢 告警已開啟</span></button>'''
        else:
            toggle_pill = f'''<button type="button" onclick="toggleEmailAlert('{sname_esc_js}')" title="點擊恢復發送異常 Email 告警" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-slate-200 text-slate-600 hover:bg-emerald-100 hover:text-emerald-800 border border-slate-300 transition cursor-pointer flex-shrink-0"><span>🔕 告警已靜音</span></button>'''

        if s.get("notification_emails"):
            email_badge = f'''
            <div class="mt-1.5 min-h-[28px] flex items-center justify-between text-[11px] text-indigo-700 bg-indigo-50/70 border border-indigo-200/60 px-2 py-0.5 rounded-md gap-1" title="異常通知收件人: {s['notification_emails']}">
                <div class="flex items-center gap-1.5 min-w-0">
                    {toggle_pill}
                    <span class="truncate font-mono">{s['notification_emails']}</span>
                </div>
                <button onclick="testEmailAlert('{sname_esc_js}')" title="點此發送測試信給該專案收件人" class="text-[10px] text-indigo-600 hover:text-indigo-900 font-semibold ml-1 flex-shrink-0 hover:underline">
                    測試
                </button>
            </div>'''
        else:
            email_badge = f'''
            <div class="mt-1.5 min-h-[28px] flex items-center justify-between text-[11px] text-slate-500 bg-slate-50 border border-slate-200/80 px-2 py-0.5 rounded-md gap-1" title="未指定獨立收件人，使用系統全域預設">
                <div class="flex items-center gap-1.5 min-w-0">
                    {toggle_pill}
                    <span class="truncate text-slate-400">使用全域收件人</span>
                </div>
                <button onclick="testEmailAlert('{sname_esc_js}')" title="點此測試寄送給全域預設收件人" class="text-[10px] text-slate-500 hover:text-slate-800 font-medium ml-1 flex-shrink-0 hover:underline">
                    測試
                </button>
            </div>'''

        # 4. 生命週期標籤按鈕化 (支援點擊查看清單，無問題時給予綠色標籤佔位保證高度齊平)
        life_badges = []
        if s.get("new_issues_count", 0) > 0:
            life_badges.append(f'''<button type="button" onclick="showIssuesModal('{sname_esc_js}', 'new', '今日新發問題')" class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold bg-rose-100 hover:bg-rose-200 text-rose-700 border border-rose-200 transition cursor-pointer" title="點擊查看今日新發問題清單">🆕 {s["new_issues_count"]} 新問題 ↗</button>''')
        if s.get("chronic_issues_count", 0) > 0:
            max_d = s.get("max_consecutive_days", 2)
            life_badges.append(f'''<button type="button" onclick="showIssuesModal('{sname_esc_js}', 'chronic', '連續頑疾 (未修復)')" class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-100 hover:bg-amber-200 text-amber-800 border border-amber-200 transition cursor-pointer" title="連續多日重複出現未修復，點擊查看清單">⏳ {s["chronic_issues_count"]} 連續頑疾 ({max_d}天) ↗</button>''')
        if s.get("acknowledged_issues_count", 0) > 0:
            life_badges.append(f'''<button type="button" onclick="showIssuesModal('{sname_esc_js}', 'acknowledged', '已知暫緩問題 (Email 靜音)')" class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold bg-slate-100 hover:bg-slate-200 text-slate-600 border border-slate-200 transition cursor-pointer" title="已標記已知待排期，點擊查看清單與暫緩原因">👁️ {s["acknowledged_issues_count"]} 已知暫緩 ↗</button>''')
        
        if not life_badges:
            lifecycle_badges_html = '<div class="flex items-center gap-1 mt-1.5 min-h-[22px]"><span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-emerald-50 text-emerald-600 border border-emerald-100">🟢 今日無新增異常</span></div>'
        else:
            lifecycle_badges_html = f'<div class="flex items-center gap-1 mt-1.5 min-h-[22px] flex-wrap">{" ".join(life_badges)}</div>'

        issues_color = "text-rose-600" if s["issues_count"] > 0 else "text-emerald-600"

        # 5. 各環境日誌分佈膠囊標籤 (預設突顯正式環境，其餘環境折疊按需展開)
        env_stats = s.get("env_stats", {})
        prod_badges = []
        other_badges = []
        if env_stats:
            for env_name, st in env_stats.items():
                e_tot = st.get("total", 0)
                e_err = st.get("error", 0)
                e_lower = env_name.lower()
                is_prod = ("prod" in e_lower or "formal" in e_lower)
                
                if is_prod:
                    if e_err > 0:
                        b_cls = "bg-rose-50 text-rose-700 border-rose-200 font-bold hover:bg-rose-100"
                        icon = "🟣"
                    else:
                        b_cls = "bg-purple-50 text-purple-700 border-purple-200 hover:bg-purple-100"
                        icon = "🟣"
                elif "dev" in e_lower:
                    if e_err > 0:
                        b_cls = "bg-amber-50 text-amber-700 border-amber-200 font-bold hover:bg-amber-100"
                        icon = "🟡"
                    else:
                        b_cls = "bg-slate-50 text-slate-700 border-slate-200 hover:bg-slate-100"
                        icon = "🟡"
                elif "local" in e_lower:
                    b_cls = "bg-emerald-50 text-emerald-700 border-emerald-200 hover:bg-emerald-100"
                    icon = "🟢"
                else:
                    b_cls = "bg-cyan-50 text-cyan-700 border-cyan-200 hover:bg-cyan-100"
                    icon = "🔵"
                    
                err_text = f"🔴 {e_err} 錯" if e_err > 0 else "🟢 正常"
                link_url = f"/logs?service={s['service_name']}&environment={env_name}&time_range=7d"
                badge_html = f'''<a href="{link_url}" title="點擊檢視 {env_name} 日誌 (近 7 天共 {e_tot} 筆)" class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-md text-[10px] font-mono border transition {b_cls}">{icon} {env_name}: {err_text}</a>'''
                
                if is_prod:
                    prod_badges.append(badge_html)
                else:
                    other_badges.append(badge_html)
                    
            if not prod_badges and other_badges:
                prod_badges.append(other_badges.pop(0))
                
            sname_key = re.sub(r'[^a-zA-Z0-9_]', '_', s['service_name'])
            prod_part = " ".join(prod_badges) if prod_badges else '<span class="text-[11px] text-slate-400 font-mono">近 7 天無正式環境日誌</span>'
            
            if other_badges:
                toggle_btn = f'''<button type="button" onclick="toggleEnvs('{sname_key}')" id="btn-toggle-envs-{sname_key}" class="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-medium text-slate-500 hover:text-slate-800 bg-slate-100 hover:bg-slate-200 border border-slate-200 rounded-md transition cursor-pointer"><span>▾ 其他環境</span></button>'''
                collapse_div = f'''<div id="other-envs-{sname_key}" class="hidden flex items-center gap-1.5 flex-wrap w-full mt-2 pt-2 border-t border-slate-100">{" ".join(other_badges)}</div>'''
                env_badges_html = f'''<div class="flex items-center gap-1.5 flex-wrap w-full">{prod_part} {toggle_btn}</div>{collapse_div}'''
            else:
                env_badges_html = f'''<div class="flex items-center gap-1.5 flex-wrap w-full">{prod_part}</div>'''
        else:
            env_badges_html = '<span class="text-[11px] text-slate-400 font-mono">近 7 天無各環境日誌</span>'

        # 6. 歷史診斷報表 (固定高度容器 h-[76px]，確保所有卡片嚴格齊平)
        history_list = ""
        if s["history"]:
            for h in s["history"][:2]:
                h_date = h['time'].split(' ')[0] if ' ' in h['time'] else h['time']
                h_icon = "🔴" if h['severity'] == "High" else "🟡"
                history_list += f'''
                <div class="flex items-center justify-between text-xs py-1 border-b border-slate-100 last:border-0 hover:bg-white/80 px-1 rounded transition">
                    <a href="/report/{s['service_name']}/{h['file']}" class="text-indigo-600 hover:text-indigo-800 hover:underline truncate max-w-[150px] font-medium flex items-center gap-1 text-[11px]">
                        <span>{h_icon}</span>
                        <span class="truncate">{h['file']}</span>
                    </a>
                    <span class="text-slate-600 font-medium text-[11px]">{h['issues']} 個問題</span>
                    <span class="text-slate-400 font-mono text-[10px]">{h_date}</span>
                </div>'''
        else:
            history_list = '<p class="text-xs text-slate-400 py-2 text-center flex items-center justify-center gap-1"><span>📄</span><span>尚無歷史診斷報告紀錄</span></p>'

        # 7. 底部 4 顆操作按鈕 (採用 2x2 統一網格 grid grid-cols-2，結構完全相同)
        # 按鈕 1: 專案總結 / 今日診斷
        if s.get("has_summary"):
            btn_col1 = f'''<a href="/report/{s['service_name']}/summary.md" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white transition shadow-sm active:scale-95 truncate"><span>📊 專案總結</span></a>'''
        elif s.get("latest_report_file"):
            if s.get("is_today_report"):
                btn_col1 = f'''<a href="/report/{s['service_name']}/{s['latest_report_file']}" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-rose-600 hover:bg-rose-700 text-white transition shadow-sm active:scale-95 truncate"><span>📝 今日診斷</span></a>'''
            else:
                btn_col1 = f'''<a href="/report/{s['service_name']}/{s['latest_report_file']}" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 transition border border-slate-200 shadow-2xs active:scale-95 truncate"><span>📝 最新診斷</span></a>'''
        else:
            btn_col1 = '''<div class="inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-emerald-50 text-emerald-700 text-xs font-medium border border-emerald-100 truncate"><span>🟢 運行正常</span></div>'''

        # 按鈕 2: GitLab 連結或綁定提示
        if s["project_path"]:
            gl_link = f"{gitlab_url.rstrip('/')}/{s['project_path']}"
            btn_col2 = f'''<a href="{gl_link}" target="_blank" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-white hover:bg-slate-50 text-slate-700 border border-slate-200 transition shadow-2xs truncate"><span>🦊 GitLab</span><span class="text-[10px] text-slate-400 font-mono">#{s['project_id'] or '-'}</span></a>'''
        else:
            btn_col2 = f'''<button onclick="openEditModal('{sname_esc_js}', '', '', '', 'main', '', '', '')" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-slate-50 hover:bg-indigo-50 text-slate-400 hover:text-indigo-600 border border-dashed border-slate-300 transition shadow-2xs truncate" title="點擊設定此服務之 GitLab 專案路徑"><span>🦊 未綁定 GitLab</span></button>'''

        # 按鈕 3: 每日報告快照
        if s.get("latest_report_file"):
            btn_col3 = f'''<a href="/report/{s['service_name']}/{s['latest_report_file']}" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 transition border border-slate-200 shadow-2xs truncate"><span>📄 診斷報告</span></a>'''
        else:
            btn_col3 = '''<span class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium rounded-lg bg-slate-50 text-slate-400 border border-slate-100 truncate"><span>📄 暫無報告</span></span>'''

        # 按鈕 4: 即時日誌探測器
        logs_env_param = f"&environment={current_env}" if current_env else ""
        explorer_url = f"/logs?service={s['service_name']}{logs_env_param}&time_range=7d"
        btn_col4 = f'''<a href="{explorer_url}" class="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 transition border border-slate-200 shadow-2xs truncate"><span>🔍 即時日誌</span></a>'''

        cards_html += f'''
        <div class="bg-white border border-slate-200/90 hover:border-slate-300 rounded-2xl p-6 shadow-sm hover:shadow-md transition-all flex flex-col justify-between">
            <div>
                <!-- 1. 卡片頂部狀態列 -->
                <div class="flex items-center justify-between gap-2 pb-3 mb-3 border-b border-slate-100">
                    <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full flex-shrink-0 {dot_class}"></span>
                        <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-bold border tracking-wide {badge_class}">
                            {status_text}
                        </span>
                    </div>
                    <div class="flex items-center gap-1">
                        {edit_btn}
                        {delete_btn}
                    </div>
                </div>

                <!-- 2. 微服務專案標題與描述 -->
                <div class="mb-4">
                    <h3 class="text-lg font-extrabold text-slate-900 tracking-tight break-all leading-snug" title="{s['service_name']}">
                        {s['service_name']}
                    </h3>
                    <p class="text-xs text-slate-500 mt-1 line-clamp-1 leading-relaxed">{s['description'] or '微服務專案'}</p>
                    {rule_badge}
                    {email_badge}
                </div>

                <!-- 3. 核心數據指標 (固定 min-h 確保高低平整) -->
                <div class="grid grid-cols-2 gap-3 my-4 p-3.5 bg-slate-50/80 rounded-xl border border-slate-100 min-h-[92px]">
                    <div>
                        <div class="text-[11px] text-slate-400 uppercase tracking-wider font-bold">今日異常數</div>
                        <div class="text-2xl font-black {issues_color} mt-0.5 flex items-baseline gap-1">
                            {s['issues_count']}
                            <span class="text-xs font-normal text-slate-500">個問題</span>
                        </div>
                        {lifecycle_badges_html}
                    </div>
                    <div>
                        <div class="text-[11px] text-slate-400 uppercase tracking-wider font-bold">監控分支</div>
                        <div class="text-sm font-semibold text-slate-700 mt-1 flex items-center gap-1">
                            <span class="px-2 py-0.5 bg-white border border-slate-200 text-indigo-700 rounded-md text-xs font-mono font-semibold shadow-2xs">⎇ {s['branch']}</span>
                        </div>
                    </div>
                </div>

                <!-- 4. 各環境現況分佈 (7天) -->
                <div class="mb-4">
                    <div class="text-[11px] font-bold text-slate-400 uppercase tracking-wider mb-1.5 flex items-center justify-between">
                        <span>運行環境分佈 (近 7 天)</span>
                        <span class="text-[10px] font-normal text-slate-400 font-mono">點擊直接探測 ↗</span>
                    </div>
                    <div class="flex items-center gap-1.5 flex-wrap">
                        {env_badges_html}
                    </div>
                </div>

                <!-- 5. 歷史診斷報表 (嚴格鎖定固定高度容器，保證卡片下沿對齊) -->
                <div class="mb-5">
                    <div class="text-[11px] font-bold text-slate-400 uppercase tracking-wider mb-2">歷史診斷報表</div>
                    <div class="bg-slate-50/50 rounded-xl px-3 py-2 border border-slate-100 h-[76px] flex flex-col justify-center overflow-hidden">
                        {history_list}
                    </div>
                </div>
            </div>

            <!-- 6. 卡片底端動作按鈕 (統一 2x2 網格，高低一致) -->
            <div class="pt-4 border-t border-slate-100 grid grid-cols-2 gap-2">
                {btn_col1}
                {btn_col2}
                {btn_col3}
                {btn_col4}
            </div>
        </div>
        '''

    if not cards_html.strip():
        cards_html = '''
        <div class="col-span-full bg-white border border-dashed border-slate-300 rounded-2xl p-12 text-center shadow-sm">
            <div class="text-4xl mb-3">📦</div>
            <h3 class="text-base font-bold text-slate-800">目前清單中尚無任何受監控微服務專案</h3>
            <p class="text-xs text-slate-500 mt-1 mb-4 max-w-sm mx-auto">點擊上方「➕ 新增監控專案」按鈕，填入微服務名稱與 GitLab 資訊即可開始智能監控。</p>
            <button onclick="openAddModal()" class="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold shadow-sm transition">
                ➕ 立即新增微服務
            </button>
        </div>
        '''

    if data['total_services'] == 0:
        global_status_banner = '''
        <div class="mb-6 p-4 rounded-xl bg-slate-100 border border-slate-200 flex items-center justify-between shadow-sm">
            <div class="flex items-center gap-3">
                <span class="text-2xl">💡</span>
                <div>
                    <h4 class="text-sm font-bold text-slate-800">尚未配置任何監控專案</h4>
                    <p class="text-xs text-slate-500">請新增微服務專案，以啟動自動化日誌巡檢與程式碼切片診斷。</p>
                </div>
            </div>
            <button onclick="openAddModal()" class="px-3.5 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold rounded-lg shadow-sm transition">
                ➕ 新增專案
            </button>
        </div>
        '''
    elif data['critical_count'] > 0:
        env_tip = f"（{current_env} 環境）" if current_env else ""
        global_status_banner = f'''
        <div class="mb-6 p-4 rounded-xl bg-rose-50 border border-rose-200 flex items-center justify-between shadow-sm">
            <div class="flex items-center gap-3">
                <span class="text-2xl">🚨</span>
                <div>
                    <h4 class="text-sm font-bold text-rose-800">系統偵測到 {data['critical_count']} 個微服務發生嚴重異常！{env_tip}</h4>
                    <p class="text-xs text-rose-600">SRE AI Agent 已完成代碼切片與根因分析，可直接點擊下方報告進行協同自癒。</p>
                </div>
            </div>
            <button onclick="runScan()" class="px-3.5 py-1.5 bg-rose-600 hover:bg-rose-700 text-white text-xs font-semibold rounded-lg shadow-sm transition">
                🔄 重新掃描
            </button>
        </div>
        '''
    elif data.get('warning_count', 0) > 0 or data.get('total_issues', 0) > 0:
        env_tip = f"（{current_env} 環境）" if current_env else ""
        global_status_banner = f'''
        <div class="mb-6 p-4 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-between shadow-sm">
            <div class="flex items-center gap-3">
                <span class="text-2xl">⚠️</span>
                <div>
                    <h4 class="text-sm font-bold text-amber-800">系統偵測到 {data['warning_count']} 個微服務存在異常警告 {env_tip}</h4>
                    <p class="text-xs text-amber-600">目前累積 {data['total_issues']} 個潛在問題，建議查閱即時日誌或評估非致命告警。</p>
                </div>
            </div>
            <button onclick="runScan()" class="px-3.5 py-1.5 bg-amber-600 hover:bg-amber-700 text-white text-xs font-semibold rounded-lg shadow-sm transition">
                🔄 重新掃描
            </button>
        </div>
        '''
    else:
        env_tip = f"（{current_env} 環境）" if current_env else ""
        global_status_banner = f'''
        <div class="mb-6 p-4 rounded-xl bg-emerald-50 border border-emerald-200 flex items-center justify-between shadow-sm">
            <div class="flex items-center gap-3">
                <span class="text-2xl">🛡️</span>
                <div>
                    <h4 class="text-sm font-bold text-emerald-800">所有受監控微服務運作健康，無阻斷性錯誤 {env_tip}</h4>
                    <p class="text-xs text-emerald-600">SRE AI Sentinel 正即時守護中，系統將依設定排程自動巡檢。</p>
                </div>
            </div>
            <div class="hidden sm:flex items-center gap-2 text-xs font-semibold text-emerald-700 bg-emerald-100/70 px-3 py-1.5 rounded-lg border border-emerald-200/80">
                <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
                <span>系統正常運行中</span>
            </div>
        </div>
        '''

    crit_color = "text-rose-600" if data['critical_count'] > 0 else "text-slate-800"
    iss_color = "text-amber-600" if data['total_issues'] > 0 else "text-emerald-600"

    # 環境切換 Tab 狀態樣式
    tab_all = "bg-indigo-600 text-white shadow-sm" if not current_env else "bg-slate-100 hover:bg-slate-200 text-slate-700"
    tab_prod = "bg-indigo-600 text-white shadow-sm" if current_env == "production" else "bg-slate-100 hover:bg-slate-200 text-slate-700"
    tab_dev = "bg-indigo-600 text-white shadow-sm" if current_env == "development" else "bg-slate-100 hover:bg-slate-200 text-slate-700"
    tab_local = "bg-indigo-600 text-white shadow-sm" if current_env == "local" else "bg-slate-100 hover:bg-slate-200 text-slate-700"
    env_subtitle = f" ｜ 目前聚焦環境：<span class='text-indigo-600 font-mono font-bold'>{current_env}</span>" if current_env else ""

    default_email = os.getenv("EMAIL_RECEIVER", "")
    if default_email:
        default_email_hint = f'💡 全域預設收件人：<span class="font-mono font-medium text-slate-700">{default_email}</span>（未指定時自動套用）'
    else:
        default_email_hint = '當此微服務發生異常時優先通知這些信箱；留空則自動使用全域預設收件人。'

    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>微服務日誌分析平台 - 異常監控與每日報告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap">
    <style>
        body {{
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f8fafc;
        }}
        .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
    </style>
</head>
<body class="text-slate-800 min-h-screen flex flex-col selection:bg-indigo-600 selection:text-white bg-slate-50">
    <!-- 頂部導覽列 (單行緊湊乾淨，絕不上下折成兩層) -->
    <header class="border-b border-slate-200 bg-white/95 backdrop-blur-md sticky top-0 z-50 shadow-xs">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between gap-4">
            <!-- 左側：品牌 Logo 與名稱 -->
            <div class="flex items-center gap-3 flex-shrink-0">
                <a href="/" class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-600 to-purple-600 flex items-center justify-center shadow-md shadow-indigo-600/20 text-white text-lg font-black hover:opacity-95 transition">
                    📊
                </a>
                <div>
                    <h1 class="text-sm sm:text-base font-bold text-slate-900 leading-none">微服務日誌分析平台</h1>
                    <span class="hidden md:inline text-[11px] text-slate-500 font-medium">異常監控與每日報告</span>
                </div>
            </div>

            <!-- 中間：分頁導航切換 (緊湊單行) -->
            <nav class="flex items-center gap-1 bg-slate-100 p-1 rounded-xl border border-slate-200 text-xs font-semibold flex-shrink-0">
                <a href="/" class="px-3 py-1.5 rounded-lg bg-white text-indigo-700 shadow-2xs flex items-center gap-1.5 whitespace-nowrap">
                    <span>📊</span>
                    <span>儀表板</span>
                </a>
                <a href="/logs" class="px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-slate-200/60 transition flex items-center gap-1.5 whitespace-nowrap">
                    <span>🔍</span>
                    <span>日誌探測</span>
                </a>
            </nav>
            
            <!-- 右側：功能按鈕組 (全部單行緊湊，嚴禁換行折斷) -->
            <div class="flex items-center gap-2 flex-shrink-0">
                <button onclick="openAddModal()" class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold transition shadow-sm active:scale-95 whitespace-nowrap">
                    <span>➕</span>
                    <span>新增專案</span>
                </button>
                <button onclick="testEmailAlert()" title="發送一封測試告警信以驗證 Email 設定" class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white hover:bg-slate-50 text-slate-700 text-xs font-semibold border border-slate-200 transition shadow-2xs active:scale-95 whitespace-nowrap">
                    <span>📧</span>
                    <span>測試信</span>
                </button>
                <button id="scanBtn" onclick="runScan()" class="flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-900 text-white text-xs font-semibold transition shadow-sm active:scale-95 whitespace-nowrap">
                    <span id="scanSpinner" class="hidden">⏳</span>
                    <span id="scanText">⚡ 立即掃描</span>
                </button>
            </div>
        </div>
    </header>

    <!-- 主要儀表板內容 -->
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-1 w-full">
        {global_status_banner}

        <!-- 運行環境過濾切換 Tab (Environment Switcher) -->
        <div class="mb-6 bg-white border border-slate-200 rounded-2xl p-3 shadow-2xs flex items-center justify-between flex-wrap gap-3">
            <div class="flex items-center gap-2 flex-wrap">
                <span class="text-xs font-bold text-slate-500 uppercase tracking-wider pl-2 pr-1">環境檢視：</span>
                <a href="/" class="px-3.5 py-1.5 text-xs font-bold rounded-xl transition {tab_all}">
                    🌐 全部環境 (ALL)
                </a>
                <a href="/?env=production" class="px-3.5 py-1.5 text-xs font-bold rounded-xl transition {tab_prod}">
                    🟣 正式環境 (production)
                </a>
                <a href="/?env=development" class="px-3.5 py-1.5 text-xs font-bold rounded-xl transition {tab_dev}">
                    🟡 開發環境 (development)
                </a>
                <a href="/?env=local" class="px-3.5 py-1.5 text-xs font-bold rounded-xl transition {tab_local}">
                    🟢 本機環境 (local)
                </a>
            </div>
            <div class="text-xs text-slate-400 font-mono pr-2 flex items-center gap-1.5">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                <span>即時日誌源：Elasticsearch logs-*{env_subtitle}</span>
            </div>
        </div>

        <!-- 總覽指標 KPI -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
            <div class="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
                <div class="text-xs text-slate-500 font-semibold uppercase tracking-wider">受監控專案總數</div>
                <div class="text-2xl font-extrabold text-slate-900 mt-1">{data['total_services']} <span class="text-xs font-normal text-slate-400">個微服務</span></div>
            </div>
            <div class="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
                <div class="text-xs text-slate-500 font-semibold uppercase tracking-wider">嚴重異常專案</div>
                <div class="text-2xl font-extrabold {crit_color} mt-1">
                    {data['critical_count']} <span class="text-xs font-normal text-slate-400">個專案</span>
                </div>
            </div>
            <div class="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
                <div class="text-xs text-slate-500 font-semibold uppercase tracking-wider">健康運行專案</div>
                <div class="text-2xl font-extrabold text-emerald-600 mt-1">
                    {data['healthy_count']} <span class="text-xs font-normal text-slate-400">個正常</span>
                </div>
            </div>
            <div class="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
                <div class="text-xs text-slate-500 font-semibold uppercase tracking-wider">累積獨立問題數</div>
                <div class="text-2xl font-extrabold {iss_color} mt-1">
                    {data['total_issues']} <span class="text-xs font-normal text-slate-400">個</span>
                </div>
            </div>
        </div>

        <!-- 微服務健康矩陣卡片清單 -->
        <div class="mb-5 flex items-center justify-between">
            <h2 class="text-lg font-bold text-slate-900 flex items-center gap-2">
                <span>📦 微服務專案健康矩陣</span>
                <span class="text-xs text-slate-500 font-normal">（可直接查閱每日切片診斷或管理配置）</span>
            </h2>
            <button onclick="openAddModal()" class="text-xs font-semibold text-indigo-600 hover:text-indigo-700 flex items-center gap-1">
                ➕ 新增專案到監控清單
            </button>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {cards_html}
        </div>
    </main>

    <!-- 頁尾 -->
    <footer class="border-t border-slate-200 py-6 text-center text-xs text-slate-500 bg-white">
        微服務日誌分析平台 ｜ 異常監控與每日報告
    </footer>

    <!-- 新增專案 Modal 彈窗 -->
    <div id="addModal" class="fixed inset-0 bg-slate-900/50 backdrop-blur-sm hidden z-50 flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-2xl max-w-lg w-full p-6 shadow-2xl transition-all">
            <div class="flex items-center justify-between pb-3 border-b border-slate-100">
                <div class="flex items-center gap-2">
                    <span class="text-xl">📦</span>
                    <h3 id="modalTitle" class="text-base font-bold text-slate-900">新增受監控微服務專案</h3>
                </div>
                <button onclick="closeAddModal()" class="text-slate-400 hover:text-slate-600 text-lg">✕</button>
            </div>

            <form id="addProjectForm" onsubmit="submitAddProject(event)" class="mt-4 space-y-4">
                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1">
                        微服務名稱 (service_name) <span class="text-rose-500">*</span>
                    </label>
                    <input type="text" id="m_service_name" required placeholder="例：billing-backend 或 socialwork2-rag-backend" 
                           class="w-full text-xs px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono">
                    <p class="text-[11px] text-slate-400 mt-1">需與業務專案在 Logstash/ELK 送出的 <code>service_name</code> 一致。</p>
                </div>

                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1">
                        專案功能中文說明
                    </label>
                    <input type="text" id="m_description" placeholder="例：社工 RAG 後端核心服務" 
                           class="w-full text-xs px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1">
                            GitLab 專案路徑
                        </label>
                        <input type="text" id="m_project_path" placeholder="例：root/isocial-rag-demo" 
                                class="w-full text-xs px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono">
                        <p class="text-[11px] text-slate-400 mt-1">貼 <code>root/isocial-rag-demo</code> 或整串 Clone 網址皆可。</p>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1">
                            GitLab Project ID
                        </label>
                        <input type="number" id="m_project_id" placeholder="例：15" 
                                class="w-full text-xs px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono">
                    </div>
                </div>

                <div>
                    <div class="flex items-center justify-between mb-1">
                        <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider">
                            監控 Git 分支
                        </label>
                        <button type="button" onclick="detectBranches()" class="text-[11px] text-indigo-600 hover:text-indigo-800 font-semibold flex items-center gap-1">
                            <span id="detectSpinner" class="hidden">⏳</span>
                            <span>🔄 探測 GitLab 分支</span>
                        </button>
                    </div>
                    <div class="flex gap-2">
                        <select id="m_branch_select" onchange="onBranchSelectChange()" class="hidden text-xs px-2.5 py-2 border border-indigo-300 bg-indigo-50/50 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono text-indigo-900 max-w-[180px]">
                            <option value="">-- 選擇探測到的分支 --</option>
                        </select>
                        <input type="text" id="m_branch" value="main" placeholder="例如: main 或 master" 
                               class="flex-1 text-xs px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono">
                    </div>
                    <p id="branchTip" class="text-[11px] text-slate-400 mt-1">輸入專案路徑後可點「探測分支」自動帶入遠端可用分支；亦可直接手動輸入。</p>
                </div>

                <div class="p-3 bg-slate-50 border border-slate-200 rounded-lg">
                    <div class="text-xs font-bold text-slate-700 uppercase tracking-wider mb-2 flex items-center gap-1.5">
                        <span>⚙️ 自訂日誌識別規則</span>
                        <span class="text-[10px] text-slate-400 font-normal">(選填，日誌若無 service_name 欄位時使用)</span>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 mb-1">
                                日誌變數名稱 (Field)
                            </label>
                            <input type="text" id="m_log_field" placeholder="例：app 或 module 或 tag" 
                                   class="w-full text-xs px-2.5 py-1.5 border border-slate-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500 font-mono">
                            <p class="text-[10px] text-slate-400 mt-0.5">留空預設走 service_name / service.name</p>
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 mb-1">
                                比對值 (Value)
                            </label>
                            <input type="text" id="m_log_value" placeholder="例：safty-alert" 
                                   class="w-full text-xs px-2.5 py-1.5 border border-slate-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500 font-mono">
                            <p class="text-[10px] text-slate-400 mt-0.5">留空預設等於上方微服務名稱</p>
                        </div>
                    </div>
                </div>

                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1 flex items-center justify-between">
                        <span>📧 專案獨立告警通知 Email (選填)</span>
                        <span class="text-[10px] text-slate-400 font-normal">輸入後按 Enter / 逗號，支援整串貼上</span>
                    </label>
                    <div id="emailTagsWrapper" onclick="focusEmailInput()" class="min-h-[42px] px-2.5 py-1.5 border border-slate-300 rounded-lg focus-within:ring-2 focus-within:ring-indigo-500 focus-within:border-indigo-500 bg-white flex flex-wrap items-center gap-1.5 cursor-text transition">
                        <div id="emailChipsList" class="flex flex-wrap items-center gap-1.5"></div>
                        <input type="text" id="m_email_input" placeholder="輸入 Email 後按 Enter 或逗號..." 
                               class="text-xs px-1 py-1 outline-none border-none bg-transparent flex-1 min-w-[170px] font-mono text-slate-800"
                               onkeydown="handleEmailKeyDown(event)"
                               onpaste="handleEmailPaste(event)"
                               onblur="handleEmailBlur()">
                    </div>
                    <input type="hidden" id="m_notification_emails" value="">
                    <div class="flex items-center justify-between mt-1 text-[11px]">
                        <p id="emailErrorTip" class="text-rose-500 font-semibold hidden">⚠️ 請輸入有效的 Email 格式 (如 user@example.com)</p>
                        <p class="text-slate-400">{default_email_hint}</p>
                    </div>
                </div>

                <div class="p-3 bg-slate-50 border border-slate-200 rounded-lg flex items-center justify-between">
                    <div>
                        <div class="text-xs font-bold text-slate-800">啟用此專案之異常 Email 告警通知</div>
                        <p class="text-[11px] text-slate-500 mt-0.5">若取消勾選，系統巡檢時即使發生異常也不會發送 Email 騷擾負責人。</p>
                    </div>
                    <label class="relative inline-flex items-center cursor-pointer">
                        <input type="checkbox" id="m_email_alert_enabled" checked class="sr-only peer">
                        <div class="w-9 h-5 bg-slate-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-indigo-600"></div>
                    </label>
                </div>

                <div class="pt-4 border-t border-slate-100 flex items-center justify-end gap-2">
                    <button type="button" onclick="closeAddModal()" class="px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100 rounded-lg transition">
                        取消
                    </button>
                    <button type="submit" id="submitBtn" class="px-4 py-2 text-xs font-semibold bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg transition shadow-sm">
                        確認儲存並加入監控
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- 生命週期異常詳情 Modal 彈窗 (點擊標籤直接看是啥) -->
    <div id="issuesModal" class="fixed inset-0 bg-slate-900/50 backdrop-blur-sm hidden z-50 flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-2xl max-w-2xl w-full p-6 shadow-2xl transition-all max-h-[85vh] flex flex-col">
            <div class="flex items-center justify-between pb-3 border-b border-slate-100 flex-shrink-0">
                <div class="flex items-center gap-2">
                    <span id="issuesModalIcon" class="text-xl">📋</span>
                    <div>
                        <h3 id="issuesModalTitle" class="text-base font-bold text-slate-900">異常問題清單</h3>
                        <p id="issuesModalSubtitle" class="text-xs text-slate-500 mt-0.5">點擊各問題可直達日誌探測進行進一步排查</p>
                    </div>
                </div>
                <button onclick="closeIssuesModal()" class="text-slate-400 hover:text-slate-600 text-lg w-7 h-7 flex items-center justify-center rounded-lg hover:bg-slate-100 transition">✕</button>
            </div>

            <div id="issuesModalBody" class="mt-4 overflow-y-auto flex-1 space-y-3 pr-1">
                <!-- 動態注入異常卡片清單 -->
            </div>

            <div class="pt-3 border-t border-slate-100 flex items-center justify-between flex-shrink-0 mt-4 text-xs">
                <span class="text-slate-400 font-mono text-[11px]">SRE Sentinel Issue State Tracker</span>
                <button type="button" onclick="closeIssuesModal()" class="px-4 py-1.5 font-semibold text-slate-700 bg-slate-100 hover:bg-slate-200 rounded-lg transition shadow-2xs">
                    關閉
                </button>
            </div>
        </div>
    </div>

    <script>
        // 注入所有專案問題詳情字典
        const allIssuesData = {issues_detail_json};

        function showIssuesModal(serviceName, issueType, categoryLabel) {{
            const sData = allIssuesData[serviceName] || {{}};
            const list = sData[issueType] || [];
            
            const titleEl = document.getElementById('issuesModalTitle');
            const subEl = document.getElementById('issuesModalSubtitle');
            const iconEl = document.getElementById('issuesModalIcon');
            const bodyEl = document.getElementById('issuesModalBody');

            let icon = '📋';
            let badgeBg = 'bg-rose-50 text-rose-700 border-rose-200';
            if (issueType === 'new') {{
                icon = '🆕';
                badgeBg = 'bg-rose-50 text-rose-700 border-rose-200';
            }} else if (issueType === 'chronic') {{
                icon = '⏳';
                badgeBg = 'bg-amber-50 text-amber-800 border-amber-200';
            }} else if (issueType === 'acknowledged') {{
                icon = '👁️';
                badgeBg = 'bg-slate-100 text-slate-700 border-slate-200';
            }}

            iconEl.innerText = icon;
            titleEl.innerHTML = `<span class="font-mono text-indigo-700">${{serviceName}}</span> ｜ ${{categoryLabel}} (${{list.length}} 個)`;
            subEl.innerText = issueType === 'acknowledged' ? '以下異常已被標記為已知技術債或排期待修，系統已對日常告警進行靜音。' : '以下為今日監控偵測到的異常類型與出現位置，可點擊跳轉查閱日誌。';

            if (!list || list.length === 0) {{
                bodyEl.innerHTML = `
                <div class="py-12 text-center text-slate-400 bg-slate-50 rounded-xl border border-dashed border-slate-200">
                    <span class="text-3xl block mb-2">✨</span>
                    <p class="text-xs font-semibold">此微服務目前在「${{categoryLabel}}」分類下無任何紀錄</p>
                </div>`;
            }} else {{
                bodyEl.innerHTML = list.map((item, idx) => {{
                    const errName = item.error_type || 'UnknownError';
                    const errLoc = item.error_location || 'unknown:0';
                    const todayCnt = item.today_count || 0;
                    const totCnt = item.total_occurrences || todayCnt;
                    const cDays = item.consecutive_days || 1;
                    const firstSeen = item.first_seen_date || '-';
                    const lastSeen = item.last_seen_date || '-';
                    const note = item.acknowledge_note ? `<div class="mt-2 text-xs bg-amber-50/80 border border-amber-200/80 text-amber-900 p-2 rounded-lg"><span class="font-bold">📝 暫緩備註：</span>${{item.acknowledge_note}}</div>` : '';
                    
                    const queryWord = encodeURIComponent(errName.split(/[:\s]/)[0]);
                    const logLink = `/logs?service=${{encodeURIComponent(serviceName)}}&query=${{queryWord}}&time_range=7d`;
                    const snameEsc = encodeURIComponent(serviceName);
                    const fpEsc = encodeURIComponent(item.fp || '');

                    const actionBtn = issueType === 'acknowledged'
                        ? `<button type="button" onclick="unackFromModal('${{snameEsc}}', '${{fpEsc}}')" class="px-2.5 py-1 text-xs font-semibold bg-white hover:bg-indigo-50 text-indigo-700 border border-indigo-200 rounded-lg shadow-2xs transition flex items-center gap-1 cursor-pointer" title="恢復每日例行告警追蹤">
                            <span>↩️</span>
                            <span>恢復追蹤</span>
                           </button>`
                        : `<button type="button" onclick="ackFromModal('${{snameEsc}}', '${{fpEsc}}')" class="px-2.5 py-1 text-xs font-semibold bg-white hover:bg-slate-100 text-slate-700 border border-slate-300 rounded-lg shadow-2xs transition flex items-center gap-1 cursor-pointer" title="標記為已知待排期，日報將不再發 Email 告警此問題">
                            <span>🔕</span>
                            <span>暫不處理</span>
                           </button>`;

                    return `
                    <div class="p-3.5 bg-slate-50/80 hover:bg-slate-100/80 rounded-xl border border-slate-200 transition">
                        <div class="flex items-start justify-between gap-3">
                            <div class="min-w-0 flex-1">
                                <div class="flex items-center gap-2 flex-wrap mb-1">
                                    <span class="font-mono font-bold text-slate-900 text-xs break-all">${{idx + 1}}. ${{errName}}</span>
                                    <span class="px-2 py-0.5 rounded text-[10px] font-bold border ${{badgeBg}} font-mono">
                                        今日 ${{todayCnt}} 次 ｜ 累計 ${{totCnt}} 次
                                    </span>
                                </div>
                                <div class="text-[11px] font-mono text-slate-500 break-all flex items-center gap-1">
                                    <span class="text-slate-400">📍 位置:</span>
                                    <span class="bg-white px-1.5 py-0.5 rounded border border-slate-200 text-slate-700">${{errLoc}}</span>
                                </div>
                                <div class="text-[11px] text-slate-400 mt-1 flex items-center gap-3">
                                    <span>初次發現: <b class="font-mono text-slate-600">${{firstSeen}}</b></span>
                                    <span>連續出現: <b class="font-mono text-slate-600">${{cDays}} 天</b></span>
                                    <span>最後發生: <b class="font-mono text-slate-600">${{lastSeen}}</b></span>
                                </div>
                                ${{note}}
                            </div>
                            <div class="flex items-center gap-1.5 flex-shrink-0">
                                ${{actionBtn}}
                                <a href="${{logLink}}" target="_blank" class="px-2.5 py-1 text-xs font-semibold bg-white hover:bg-indigo-50 text-indigo-700 border border-indigo-200 rounded-lg shadow-2xs transition flex items-center gap-1" title="前往即時日誌探測器查詢此異常">
                                    <span>🔍</span>
                                    <span>查日誌</span>
                                </a>
                            </div>
                        </div>
                    </div>
                    `;
                }}).join('');
            }}

            document.getElementById('issuesModal').classList.remove('hidden');
        }}

        function closeIssuesModal() {{
            document.getElementById('issuesModal').classList.add('hidden');
        }}

        async function ackFromModal(encodedServiceName, encodedFp) {{
            const serviceName = decodeURIComponent(encodedServiceName);
            const fp = decodeURIComponent(encodedFp);
            const note = prompt("請輸入已知備註（選填，例如：Q4重構再處理、等待外部API恢復）：", "已知問題，暫緩處理");
            if (note === null) return;
            try {{
                const res = await fetch('/api/issues/acknowledge', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{service_name: serviceName, fingerprint: fp, note: note}})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ ' + data.message);
                    window.location.reload();
                }} else {{
                    alert('❌ 標記失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (e) {{
                alert('❌ 請求失敗: ' + e);
            }}
        }}

        async function unackFromModal(encodedServiceName, encodedFp) {{
            const serviceName = decodeURIComponent(encodedServiceName);
            const fp = decodeURIComponent(encodedFp);
            if (!confirm("確定要將此問題恢復為正常告警追蹤嗎？")) return;
            try {{
                const res = await fetch('/api/issues/unacknowledge', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{service_name: serviceName, fingerprint: fp}})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ ' + data.message);
                    window.location.reload();
                }} else {{
                    alert('❌ 恢復失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (e) {{
                alert('❌ 請求失敗: ' + e);
            }}
        }}
        let emailList = [];

        function toggleEnvs(snameKey) {{
            const el = document.getElementById('other-envs-' + snameKey);
            const btn = document.getElementById('btn-toggle-envs-' + snameKey);
            if (!el || !btn) return;
            if (el.classList.contains('hidden')) {{
                el.classList.remove('hidden');
                btn.innerHTML = '<span>▴ 收起</span>';
            }} else {{
                el.classList.add('hidden');
                btn.innerHTML = '<span>▾ 其他環境</span>';
            }}
        }}

        function focusEmailInput() {{
            document.getElementById('m_email_input').focus();
        }}

        function validateEmail(email) {{
            return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email.trim());
        }}

        function renderEmailTags() {{
            const listEl = document.getElementById('emailChipsList');
            const inputEl = document.getElementById('m_email_input');
            const hiddenEl = document.getElementById('m_notification_emails');
            listEl.innerHTML = '';

            emailList.forEach((email, idx) => {{
                const chip = document.createElement('span');
                chip.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-indigo-50 text-indigo-700 border border-indigo-200 text-xs font-mono font-medium shadow-2xs group transition';
                chip.innerHTML = `
                    <span>✉️</span>
                    <span>${{email}}</span>
                    <button type="button" onclick="removeEmailTag(${{idx}})" class="text-indigo-400 hover:text-rose-600 hover:bg-rose-50 rounded p-0.5 transition ml-0.5 font-bold" title="移除此收件人">✕</button>
                `;
                listEl.appendChild(chip);
            }});

            hiddenEl.value = emailList.join(', ');
            inputEl.placeholder = emailList.length > 0 ? '繼續新增...' : '輸入 Email 後按 Enter 或逗號...';
        }}

        function addEmailTag(raw) {{
            const errTip = document.getElementById('emailErrorTip');
            const trimmed = raw.trim().replace(/^[,;，；\\s]+|[,;，；\\s]+$/g, '');
            if (!trimmed) return false;

            if (!validateEmail(trimmed)) {{
                errTip.classList.remove('hidden');
                setTimeout(() => {{ errTip.classList.add('hidden'); }}, 3500);
                return false;
            }}

            errTip.classList.add('hidden');
            if (!emailList.includes(trimmed)) {{
                emailList.push(trimmed);
                renderEmailTags();
            }}
            return true;
        }}

        function removeEmailTag(idx) {{
            if (idx >= 0 && idx < emailList.length) {{
                emailList.splice(idx, 1);
                renderEmailTags();
            }}
        }}

        function handleEmailKeyDown(e) {{
            const input = e.target;
            const errTip = document.getElementById('emailErrorTip');
            
            if (e.key === 'Enter' || e.key === ',' || e.key === ';' || e.key === 'Tab') {{
                if (input.value.trim()) {{
                    e.preventDefault();
                    if (addEmailTag(input.value)) {{
                        input.value = '';
                    }}
                }} else if (e.key === 'Enter') {{
                    // 輸入框為空時按 Enter 不做特殊動作
                }}
            }} else if (e.key === 'Backspace' && !input.value && emailList.length > 0) {{
                emailList.pop();
                renderEmailTags();
            }} else {{
                errTip.classList.add('hidden');
            }}
        }}

        function handleEmailPaste(e) {{
            e.preventDefault();
            const pasteData = (e.clipboardData || window.clipboardData).getData('text');
            if (!pasteData) return;
            // 支援逗號、分號、空格、換行分隔
            const tokens = pasteData.split(/[,;，；\\s\\n\\r]+/);
            tokens.forEach(t => {{
                if (t.trim()) addEmailTag(t);
            }});
            e.target.value = '';
        }}

        function handleEmailBlur() {{
            const input = document.getElementById('m_email_input');
            if (input && input.value.trim()) {{
                if (addEmailTag(input.value)) {{
                    input.value = '';
                }}
            }}
        }}

        function openAddModal() {{
            document.getElementById('modalTitle').innerText = '📦 新增受監控微服務專案';
            document.getElementById('m_service_name').value = '';
            document.getElementById('m_service_name').readOnly = false;
            document.getElementById('m_service_name').classList.remove('bg-slate-100');
            document.getElementById('m_description').value = '';
            document.getElementById('m_project_path').value = '';
            document.getElementById('m_project_id').value = '';
            document.getElementById('m_branch').value = 'main';
            document.getElementById('m_log_field').value = '';
            document.getElementById('m_log_value').value = '';
            document.getElementById('m_notification_emails').value = '';
            document.getElementById('m_email_input').value = '';
            document.getElementById('m_email_alert_enabled').checked = true;
            document.getElementById('emailErrorTip').classList.add('hidden');
            emailList = [];
            renderEmailTags();
            document.getElementById('addModal').classList.remove('hidden');
        }}

        function openEditModal(serviceName, description, projectPath, projectId, branch, logField, logValue, notificationEmails, emailAlertEnabled) {{
            document.getElementById('modalTitle').innerText = '✏️ 編輯 / 註冊微服務配置';
            document.getElementById('m_service_name').value = serviceName;
            document.getElementById('m_service_name').readOnly = true;
            document.getElementById('m_service_name').classList.add('bg-slate-100');
            document.getElementById('m_description').value = (description && !description.includes('未註冊')) ? description : '';
            document.getElementById('m_project_path').value = projectPath || '';
            document.getElementById('m_project_id').value = projectId || '';
            document.getElementById('m_branch').value = branch || 'main';
            document.getElementById('m_log_field').value = logField || '';
            document.getElementById('m_log_value').value = logValue || '';
            document.getElementById('m_email_input').value = '';
            document.getElementById('m_email_alert_enabled').checked = (emailAlertEnabled !== false);
            document.getElementById('emailErrorTip').classList.add('hidden');
            
            emailList = [];
            if (notificationEmails) {{
                notificationEmails.split(/[,;，；]/).forEach(item => {{
                    const clean = item.trim();
                    if (clean && !emailList.includes(clean)) {{
                        emailList.push(clean);
                    }}
                }});
            }}
            renderEmailTags();
            document.getElementById('addModal').classList.remove('hidden');
        }}

        function closeAddModal() {{
            document.getElementById('addModal').classList.add('hidden');
        }}

        async function toggleEmailAlert(serviceName) {{
            try {{
                const res = await fetch('/api/projects/' + encodeURIComponent(serviceName) + '/toggle_email', {{
                    method: 'POST'
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    window.location.reload();
                }} else {{
                    alert('❌ 切換告警狀態失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (err) {{
                alert('❌ 網路請求失敗: ' + err);
            }}
        }}

        async function detectBranches() {{
            const pathOrId = document.getElementById('m_project_path').value.trim() || document.getElementById('m_project_id').value.trim();
            if (!pathOrId) {{
                alert('⚠️ 請先填寫「GitLab 專案路徑」或「Project ID」，再點擊探測分支！');
                return;
            }}
            const spinner = document.getElementById('detectSpinner');
            const tip = document.getElementById('branchTip');
            const select = document.getElementById('m_branch_select');
            spinner.classList.remove('hidden');
            tip.innerText = '正在向 GitLab 查詢遠端分支...';

            try {{
                const res = await fetch('/api/gitlab/branches?project_path=' + encodeURIComponent(pathOrId));
                const data = await res.json();
                if (data.status === 'ok' && data.branches && data.branches.length > 0) {{
                    select.innerHTML = '<option value="">-- 選擇探測到的分支 --</option>';
                    data.branches.forEach(b => {{
                        select.innerHTML += `<option value="${{b}}">${{b}}</option>`;
                    }});
                    select.classList.remove('hidden');
                    tip.innerHTML = `✅ 成功探測到 <b>${{data.branches.length}}</b> 個遠端分支，請自下拉選單快速選取。`;
                }} else {{
                    tip.innerHTML = '<span class="text-amber-600">⚠️ 無法取得分支清單 (可能 Token 過期或權限不足)，請手動於右側輸入。</span>';
                }}
            }} catch (err) {{
                tip.innerHTML = '<span class="text-rose-600">❌ 探測失敗，請直接手動輸入分支名稱。</span>';
            }} finally {{
                spinner.classList.add('hidden');
            }}
        }}

        function onBranchSelectChange() {{
            const select = document.getElementById('m_branch_select');
            if (select.value) {{
                document.getElementById('m_branch').value = select.value;
            }}
        }}

        async function submitAddProject(e) {{
            e.preventDefault();
            const trailingEmail = document.getElementById('m_email_input').value.trim();
            if (trailingEmail) {{
                addEmailTag(trailingEmail);
                document.getElementById('m_email_input').value = '';
            }}

            const submitBtn = document.getElementById('submitBtn');
            submitBtn.disabled = true;
            submitBtn.innerText = '儲存中...';

            const payload = {{
                service_name: document.getElementById('m_service_name').value.trim(),
                description: document.getElementById('m_description').value.trim(),
                project_path: document.getElementById('m_project_path').value.trim(),
                project_id: document.getElementById('m_project_id').value ? parseInt(document.getElementById('m_project_id').value) : null,
                branch: document.getElementById('m_branch').value.trim() || 'main',
                log_field: document.getElementById('m_log_field').value.trim(),
                log_value: document.getElementById('m_log_value').value.trim(),
                notification_emails: emailList.join(', '),
                email_alert_enabled: document.getElementById('m_email_alert_enabled').checked
            }};

            try {{
                const res = await fetch('/api/projects', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('🎉 專案 [' + payload.service_name + '] 已成功新增至監控清單！');
                    closeAddModal();
                    window.location.reload();
                }} else {{
                    alert('❌ 新增失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (err) {{
                alert('❌ 網路請求失敗: ' + err);
            }} finally {{
                submitBtn.disabled = false;
                submitBtn.innerText = '確認儲存並加入監控';
            }}
        }}

        async function deleteProject(serviceName) {{
            if (!confirm('⚠️ 確定要將微服務專案 [' + serviceName + '] 從監控清單中移除嗎？')) {{
                return;
            }}
            try {{
                const res = await fetch('/api/projects/' + encodeURIComponent(serviceName), {{
                    method: 'DELETE'
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ 專案已順利自監控清單移除！');
                    window.location.reload();
                }} else {{
                    alert('❌ 移除失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (err) {{
                alert('❌ 請求失敗: ' + err);
            }}
        }}

        async function runScan() {{
            const btn = document.getElementById('scanBtn');
            const spinner = document.getElementById('scanSpinner');
            const text = document.getElementById('scanText');
            btn.disabled = true;
            spinner.classList.remove('hidden');
            text.innerText = '正在掃描與分析日誌...';
            
            try {{
                const res = await fetch('/api/scan', {{ method: 'POST' }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ 掃描與診斷完成！頁面即將重新整理。');
                    window.location.reload();
                }} else {{
                    alert('⚠️ 掃描返回: ' + (data.message || '掃描完成'));
                    window.location.reload();
                }}
            }} catch (e) {{
                alert('❌ 呼叫掃描失敗: ' + e);
            }} finally {{
                btn.disabled = false;
                spinner.classList.add('hidden');
                text.innerText = '⚡ 立即掃描日誌';
            }}
        }}

        async function testEmailAlert(serviceName) {{
            const targetPrompt = serviceName ? `是否發送專案 [${{serviceName}}] 的測試告警信給其專屬負責人？` : '是否立即發送一封測試告警信到設定的收件人信箱？';
            if (!confirm(targetPrompt)) return;
            try {{
                const url = serviceName ? `/api/test/email?service_name=${{encodeURIComponent(serviceName)}}` : '/api/test/email';
                const res = await fetch(url, {{ method: 'POST' }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ ' + data.message);
                }} else {{
                    alert('❌ ' + data.message);
                }}
            }} catch (e) {{
                alert('❌ 呼叫測試失敗: ' + e);
            }}
        }}
    </script>
</body>
</html>'''

def render_report_view_html(service_name: str, filename: str, md_content: str) -> str:
    escaped_md = json.dumps(md_content)
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{service_name} - {filename} 診斷報表</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap">
    <style>
        body {{
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f8fafc;
        }}
        .markdown-body {{
            color: #334155;
            line-height: 1.75;
        }}
        .markdown-body h1, .markdown-body h2, .markdown-body h3 {{
            color: #0f172a;
            font-weight: 700;
            margin-top: 1.75rem;
            margin-bottom: 0.85rem;
        }}
        .markdown-body h1 {{ font-size: 1.75rem; border-bottom: 1px solid #e2e8f0; padding-bottom: 0.5rem; }}
        .markdown-body h2 {{ font-size: 1.35rem; border-bottom: 1px solid #f1f5f9; padding-bottom: 0.4rem; }}
        .markdown-body h3 {{ font-size: 1.15rem; }}
        .markdown-body pre {{
            background-color: #f1f5f9;
            border: 1px solid #e2e8f0;
            border-radius: 0.75rem;
            padding: 1.1rem;
            overflow-x: auto;
            margin: 1.25rem 0;
        }}
        .markdown-body code {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.875rem;
        }}
        .markdown-body table {{
            width: 100%;
            border-collapse: collapse;
            margin: 1.25rem 0;
            background-color: #ffffff;
            border-radius: 0.5rem;
            overflow: hidden;
            box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
        }}
        .markdown-body th, .markdown-body td {{
            border: 1px solid #e2e8f0;
            padding: 0.6rem 0.85rem;
            text-align: left;
        }}
        .markdown-body th {{
            background-color: #f8fafc;
            color: #1e293b;
            font-weight: 600;
        }}
        .markdown-body blockquote {{
            border-left: 4px solid #6366f1;
            padding-left: 1rem;
            color: #475569;
            margin: 1.25rem 0;
            background: #eef2ff;
            padding-top: 0.6rem;
            padding-bottom: 0.6rem;
            border-radius: 0 0.5rem 0.5rem 0;
        }}
        .markdown-body a {{
            color: #4f46e5;
            text-decoration: underline;
            font-weight: 500;
        }}
    </style>
</head>
<body class="text-slate-800 min-h-screen flex flex-col bg-slate-50">
    <header class="border-b border-slate-200 bg-white/90 backdrop-blur-md sticky top-0 z-50 shadow-sm">
        <div class="max-w-5xl mx-auto px-4 h-16 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <a href="/" class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-200 transition">
                    ⬅️ 返回全域監控儀表板
                </a>
                <span class="text-slate-300">/</span>
                <span class="text-xs font-bold text-indigo-700 font-mono">{service_name}</span>
                <span class="text-slate-300">/</span>
                <span class="text-xs text-slate-500 font-mono">{filename}</span>
            </div>
            <div>
                <button onclick="window.print()" class="px-3 py-1.5 text-xs font-medium bg-white hover:bg-slate-50 rounded-lg text-slate-700 border border-slate-200 shadow-sm transition">
                    🖨️ 列印報告
                </button>
            </div>
        </div>
    </header>

    <main class="max-w-5xl mx-auto px-4 py-8 flex-1 w-full">
        <div id="content" class="markdown-body bg-white border border-slate-200 rounded-2xl p-6 sm:p-10 shadow-sm">
        </div>
    </main>

    <footer class="border-t border-slate-200 py-6 text-center text-xs text-slate-500 bg-white">
        微服務日誌分析平台 ｜ 異常監控與每日報告
    </footer>

    <script>
        const rawMd = {escaped_md};
        marked.setOptions({{
            highlight: function(code, lang) {{
                const language = hljs.getLanguage(lang) ? lang : 'plaintext';
                return hljs.highlight(code, {{ language }}).value;
            }}
        }});
        document.getElementById('content').innerHTML = marked.parse(rawMd);

        async function acknowledgeIssue(serviceName, fingerprint) {{
            const note = prompt("請輸入已知備註（選填，例如：Q4重構再處理、慢查詢已建索引）：", "已知問題，暫緩處理");
            if (note === null) return;
            try {{
                const res = await fetch('/api/issues/acknowledge', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{service_name: serviceName, fingerprint: fingerprint, note: note}})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ ' + data.message);
                    window.location.reload();
                }} else {{
                    alert('❌ 標記失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (e) {{
                alert('❌ 請求失敗: ' + e);
            }}
        }}

        async function unacknowledgeIssue(serviceName, fingerprint) {{
            if (!confirm("確定要將此問題恢復為正常告警追蹤嗎？")) return;
            try {{
                const res = await fetch('/api/issues/unacknowledge', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{service_name: serviceName, fingerprint: fingerprint}})
                }});
                const data = await res.json();
                if (data.status === 'ok') {{
                    alert('✅ ' + data.message);
                    window.location.reload();
                }} else {{
                    alert('❌ 恢復失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (e) {{
                alert('❌ 請求失敗: ' + e);
            }}
        }}
    </script>
</body>
</html>'''


def render_log_explorer_html(projects_cfg: dict, default_service: str = "", default_env: str = "", default_time_range: str = "") -> str:
    """
    渲染即時日誌探測器 (Log Explorer) 頁面
    提供專案篩選、靈活時間範圍、等級選擇、關鍵字搜尋與即時 JSON 展開查看
    """
    services_options = '<option value="">-- 全部專案 (All Services) --</option>'
    for sname, conf in projects_cfg.items():
        selected = 'selected' if sname == default_service else ''
        field_info = f" ({conf.get('log_field')})" if conf.get('log_field') else ""
        services_options += f'<option value="{sname}" {selected}>{sname}{field_info}</option>'

    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>即時日誌探測器 - 微服務日誌分析平台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap">
    <style>
        body {{
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f8fafc;
        }}
        .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
        pre code {{ font-family: 'JetBrains Mono', monospace; }}
    </style>
</head>
<body class="text-slate-800 min-h-screen flex flex-col bg-slate-50 selection:bg-indigo-600 selection:text-white">
    <!-- 頂部導覽列 -->
    <header class="border-b border-slate-200 bg-white/90 backdrop-blur-md sticky top-0 z-50 shadow-sm">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <a href="/" class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-600 to-purple-600 flex items-center justify-center shadow-md shadow-indigo-600/20 text-white text-lg font-black hover:opacity-95 transition">
                    📊
                </a>
                <div>
                    <h1 class="text-base font-bold text-slate-900 leading-none">微服務日誌分析平台</h1>
                    <span class="hidden md:inline text-[11px] text-slate-500 font-medium">即時日誌探測器</span>
                </div>
            </div>

            <!-- 分頁導航切換 (單行緊湊) -->
            <nav class="flex items-center gap-1 bg-slate-100 p-1 rounded-xl border border-slate-200 text-xs font-semibold flex-shrink-0">
                <a href="/" class="px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-slate-200/60 transition flex items-center gap-1.5 whitespace-nowrap">
                    <span>📊</span>
                    <span>儀表板</span>
                </a>
                <a href="/logs" class="px-3 py-1.5 rounded-lg bg-white text-indigo-700 shadow-2xs flex items-center gap-1.5 whitespace-nowrap">
                    <span>🔍</span>
                    <span>日誌探測</span>
                </a>
            </nav>

            <div class="flex items-center gap-2 flex-shrink-0">
                <a href="/" class="px-3 py-1.5 text-xs font-semibold text-slate-600 hover:text-slate-900 bg-slate-100 hover:bg-slate-200 rounded-lg transition border border-slate-200 whitespace-nowrap">
                    ⬅️ 返回儀表板
                </a>
            </div>
        </div>
    </header>

    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-1 w-full">
        <!-- 搜尋與過濾條件工具卡片 -->
        <div class="bg-white border border-slate-200 rounded-2xl p-5 sm:p-6 shadow-sm mb-6">
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4 mb-4">
                <!-- 1. 專案選擇 -->
                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">微服務專案</label>
                    <select id="serviceSelect" onchange="onServiceChanged()" class="w-full text-xs font-medium border border-slate-300 rounded-lg px-3 py-2 bg-white text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        {services_options}
                    </select>
                </div>

                <!-- 2. 運行環境 (Environment) -->
                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">運行環境 (Environment)</label>
                    <select id="envSelect" onchange="queryLogs()" class="w-full text-xs font-medium border border-slate-300 rounded-lg px-3 py-2 bg-white text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        <option value="">全部環境 (ALL)</option>
                        <option value="production">production (正式)</option>
                        <option value="development">development (開發)</option>
                        <option value="local">local (本機)</option>
                    </select>
                </div>

                <!-- 3. 時間區間 -->
                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">時間範圍</label>
                    <select id="timeRangeSelect" onchange="toggleCustomTime()" class="w-full text-xs font-medium border border-slate-300 rounded-lg px-3 py-2 bg-white text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        <option value="1h">過去 1 小時</option>
                        <option value="6h">過去 6 小時</option>
                        <option value="24h" selected>過去 24 小時</option>
                        <option value="48h">過去 2 天</option>
                        <option value="7d">過去 7 天</option>
                        <option value="custom">自訂時間範圍...</option>
                    </select>
                </div>

                <!-- 4. 日誌等級 -->
                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">日誌等級 (Level)</label>
                    <select id="levelSelect" class="w-full text-xs font-medium border border-slate-300 rounded-lg px-3 py-2 bg-white text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        <option value="">全部等級 (ALL)</option>
                        <option value="ERROR" selected>ERROR / 嚴重錯誤</option>
                        <option value="WARN">WARN / 警告</option>
                        <option value="INFO">INFO / 資訊</option>
                        <option value="DEBUG">DEBUG / 除錯</option>
                    </select>
                </div>

                <!-- 5. 筆數上限 -->
                <div>
                    <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">最大抓取筆數</label>
                    <select id="limitSelect" class="w-full text-xs font-medium border border-slate-300 rounded-lg px-3 py-2 bg-white text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500">
                        <option value="50">最新 50 筆</option>
                        <option value="100" selected>最新 100 筆</option>
                        <option value="200">最新 200 筆</option>
                        <option value="500">最新 500 筆</option>
                    </select>
                </div>
            </div>

            <!-- 自訂時間輸入區 (預設隱藏) -->
            <div id="customTimeArea" class="hidden grid grid-cols-1 sm:grid-cols-2 gap-4 pt-3 pb-2 border-t border-slate-100 mb-3">
                <div>
                    <label class="block text-xs font-bold text-slate-600 mb-1">起始時間 (Start Time)</label>
                    <input type="datetime-local" id="startTimeInput" class="w-full text-xs border border-slate-300 rounded-lg px-3 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
                <div>
                    <label class="block text-xs font-bold text-slate-600 mb-1">結束時間 (End Time)</label>
                    <input type="datetime-local" id="endTimeInput" class="w-full text-xs border border-slate-300 rounded-lg px-3 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
            </div>

            <!-- 關鍵字搜尋列與查詢按鈕 -->
            <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 pt-3 border-t border-slate-100">
                <div class="relative flex-1">
                    <span class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-slate-400">
                        🔍
                    </span>
                    <input type="text" id="keywordInput" onkeydown="if(event.key==='Enter') queryLogs()" placeholder="搜尋關鍵字 (例如: 500, Exception, timeout, visit)..." class="w-full pl-9 pr-4 py-2.5 text-xs font-mono border border-slate-300 rounded-xl focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
                <button id="searchBtn" onclick="queryLogs()" class="flex items-center justify-center gap-2 px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-sm transition active:scale-95">
                    <span id="searchSpinner" class="hidden">⏳</span>
                    <span>執行探測查詢</span>
                </button>
            </div>
        </div>

        <!-- 日誌時間趨勢長條圖 (Log Volume Histogram) 卡片 -->
        <div id="histogramCard" class="bg-white border border-slate-200 rounded-2xl p-5 shadow-sm mb-6">
            <div class="flex items-center justify-between mb-3">
                <div class="flex items-center gap-2">
                    <span class="text-sm">📈</span>
                    <h3 class="text-xs font-bold text-slate-800 uppercase tracking-wider">日誌時間分佈趨勢 (Log Volume Histogram)</h3>
                </div>
                <div class="flex items-center gap-3 text-[11px] font-mono">
                    <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-xs bg-rose-500"></span><span class="text-slate-600 font-semibold">ERROR</span></span>
                    <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-xs bg-amber-400"></span><span class="text-slate-600 font-semibold">WARN</span></span>
                    <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-xs bg-emerald-500"></span><span class="text-slate-600 font-semibold">INFO</span></span>
                </div>
            </div>
            <!-- 長條圖繪製區 (完美自適應寬度，不再產生橫向捲軸) -->
            <div id="histogramContainer" class="h-24 w-full flex items-end justify-between gap-1 pt-3 pb-1 border-b border-slate-100">
                <div class="text-xs text-slate-400 m-auto">點擊查詢即可繪製時序趨勢長條圖</div>
            </div>
            <div class="flex justify-between text-[10px] text-slate-400 font-mono mt-1 px-1" id="histogramTimeAxis">
                <span>-</span>
                <span>即時分佈</span>
                <span>現在</span>
            </div>
        </div>

        <!-- 狀態列與結果統計 -->
        <div class="flex items-center justify-between mb-4">
            <div class="flex items-center gap-2">
                <span class="text-xs font-bold text-slate-700 uppercase tracking-wider">查詢結果</span>
                <span id="resultCountBadge" class="px-2.5 py-0.5 rounded-full text-xs font-mono font-bold bg-slate-200 text-slate-700">0 筆</span>
            </div>
            <div id="queryMeta" class="text-xs text-slate-400 font-mono">
                尚未執行查詢
            </div>
        </div>

        <!-- 日誌結果列表容器 -->
        <div id="logsContainer" class="space-y-3">
            <div class="bg-white border border-slate-200 rounded-2xl p-12 text-center text-slate-400">
                <span class="text-4xl block mb-2">📋</span>
                <p class="text-sm font-semibold text-slate-600">準備就緒，請點擊上方「執行探測查詢」撈取即時日誌</p>
                <p class="text-xs text-slate-400 mt-1">支援指定微服務專案、自訂時間區段與錯誤等級過濾</p>
            </div>
        </div>
    </main>

    <!-- 原始日誌詳細資訊 Modal -->
    <div id="logDetailModal" class="hidden fixed inset-0 z-50 overflow-y-auto bg-slate-900/60 backdrop-blur-xs flex items-center justify-center p-4">
        <div class="bg-white rounded-2xl max-w-4xl w-full p-6 shadow-2xl border border-slate-200 flex flex-col max-h-[90vh]">
            <div class="flex items-center justify-between pb-4 border-b border-slate-100">
                <div class="flex items-center gap-2">
                    <span class="text-lg">📄</span>
                    <h3 class="text-sm font-bold text-slate-900 font-mono">日誌完整資料結構 (Raw JSON)</h3>
                </div>
                <button onclick="closeDetailModal()" class="text-slate-400 hover:text-slate-600 text-xl font-bold p-1">&times;</button>
            </div>
            <div class="overflow-y-auto flex-1 my-4">
                <pre id="modalJsonContent" class="text-xs font-mono p-4 bg-slate-900 text-slate-100 rounded-xl overflow-x-auto whitespace-pre-wrap"></pre>
            </div>
            <div class="pt-3 border-t border-slate-100 flex justify-end gap-2">
                <button onclick="copyModalJson()" class="px-4 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-semibold rounded-lg transition">
                    📋 複製 JSON
                </button>
                <button onclick="closeDetailModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-900 text-white text-xs font-semibold rounded-lg transition">
                    關閉
                </button>
            </div>
        </div>
    </div>

    <footer class="border-t border-slate-200 py-6 text-center text-xs text-slate-500 bg-white">
        SRE AI Sentinel Observability Platform ｜ 即時微服務日誌探測模組
    </footer>

    <script>
        let currentLogs = [];

        function toggleCustomTime() {{
            const val = document.getElementById('timeRangeSelect').value;
            const area = document.getElementById('customTimeArea');
            if (val === 'custom') {{
                area.classList.remove('hidden');
            }} else {{
                area.classList.add('hidden');
            }}
        }}

        async function loadEnvironments(preferredEnv = null) {{
            const svc = document.getElementById('serviceSelect').value;
            const envSelect = document.getElementById('envSelect');
            const curVal = preferredEnv || envSelect.value;
            try {{
                const url = svc ? `/api/logs/environments?service_name=${{encodeURIComponent(svc)}}` : '/api/logs/environments';
                const res = await fetch(url);
                const data = await res.json();
                if (data.status === 'ok' && data.environments && data.environments.length > 0) {{
                    let opts = '<option value="">全部環境 (ALL)</option>';
                    let foundCur = false;
                    data.environments.forEach(e => {{
                        const isSel = curVal && (e.toLowerCase() === curVal.toLowerCase());
                        if (isSel) foundCur = true;
                        const selAttr = isSel ? 'selected' : '';
                        let label = e;
                        if (e === 'production') label = 'production (正式)';
                        else if (e === 'development') label = 'development (開發)';
                        else if (e === 'local') label = 'local (本機)';
                        opts += `<option value="${{escapeHtml(e)}}" ${{selAttr}}>${{escapeHtml(label)}}</option>`;
                    }});
                    // 若外部傳入的環境不在 ES 返回列表中，動態加入確保能精準過濾
                    if (curVal && !foundCur) {{
                        opts += `<option value="${{escapeHtml(curVal)}}" selected>${{escapeHtml(curVal)}}</option>`;
                    }}
                    envSelect.innerHTML = opts;
                    if (curVal) envSelect.value = curVal;
                }}
            }} catch (err) {{
                console.warn('載入環境選單失敗:', err);
            }}
        }}

        function onServiceChanged() {{
            loadEnvironments();
            queryLogs();
        }}

        async function queryLogs() {{
            const service = document.getElementById('serviceSelect').value;
            const env = document.getElementById('envSelect').value;
            const timeRange = document.getElementById('timeRangeSelect').value;
            const level = document.getElementById('levelSelect').value;
            const limit = document.getElementById('limitSelect').value;
            const keyword = document.getElementById('keywordInput').value.trim();

            let startTime = '';
            let endTime = '';
            let hours = 24;

            if (timeRange === 'custom') {{
                startTime = document.getElementById('startTimeInput').value;
                endTime = document.getElementById('endTimeInput').value;
            }} else if (timeRange === '1h') hours = 1;
            else if (timeRange === '6h') hours = 6;
            else if (timeRange === '24h') hours = 24;
            else if (timeRange === '48h') hours = 48;
            else if (timeRange === '7d') hours = 168;

            const btn = document.getElementById('searchBtn');
            const spinner = document.getElementById('searchSpinner');
            btn.disabled = true;
            spinner.classList.remove('hidden');

            const params = new URLSearchParams({{
                limit: limit
            }});
            if (service) params.append('service_name', service);
            if (env) params.append('environment', env);
            if (level) params.append('level', level);
            if (keyword) params.append('keyword', keyword);

            if (timeRange === 'custom') {{
                if (startTime) params.append('start_time', startTime);
                if (endTime) params.append('end_time', endTime);
            }} else {{
                params.append('hours', hours);
            }}

            try {{
                const resp = await fetch(`/api/logs/query?${{params.toString()}}`);
                const data = await resp.json();

                if (data.status === 'ok') {{
                    currentLogs = data.logs || [];
                    renderLogItems(currentLogs);
                    document.getElementById('resultCountBadge').innerText = `${{currentLogs.length}} 筆`;
                    document.getElementById('queryMeta').innerText = `耗時 ${{data.elapsed_ms || 0}} ms ｜ 查詢索引: ${{data.index_name}}`;
                    // 同步觸發長條圖趨勢繪製
                    fetchHistogram(params);
                }} else {{
                    alert('查詢日誌失敗: ' + (data.message || '未知錯誤'));
                }}
            }} catch (err) {{
                alert('網路或伺服器異常: ' + err);
            }} finally {{
                btn.disabled = false;
                spinner.classList.add('hidden');
            }}
        }}

        async function fetchHistogram(searchParams) {{
            const histContainer = document.getElementById('histogramContainer');
            try {{
                const res = await fetch(`/api/logs/histogram?${{searchParams.toString()}}`);
                const data = await res.json();
                if (data.status === 'ok' && data.buckets && data.buckets.length > 0) {{
                    renderHistogram(data.buckets);
                }} else {{
                    histContainer.innerHTML = '<div class="text-xs text-slate-400 m-auto">此時段無日誌聚合數據</div>';
                }}
            }} catch (e) {{
                histContainer.innerHTML = '<div class="text-xs text-slate-400 m-auto">長條圖載入異常</div>';
            }}
        }}

        function formatTimeLabel(isoStr) {{
            if (!isoStr) return '-';
            try {{
                const d = new Date(isoStr);
                const m = String(d.getMonth() + 1).padStart(2, '0');
                const day = String(d.getDate()).padStart(2, '0');
                const h = String(d.getHours()).padStart(2, '0');
                const min = String(d.getMinutes()).padStart(2, '0');
                return `${{m}}/${{day}} ${{h}}:${{min}}`;
            }} catch(e) {{
                return isoStr.replace('T', ' ').slice(5, 16);
            }}
        }}

        function renderHistogram(buckets) {{
            const container = document.getElementById('histogramContainer');
            const axis = document.getElementById('histogramTimeAxis');
            if (!buckets || buckets.length === 0) {{
                container.innerHTML = '<div class="text-xs text-slate-400 m-auto">所選區間內無日誌統計數據</div>';
                return;
            }}

            const maxTotal = Math.max(...buckets.map(b => b.total), 1);
            let barsHtml = '';

            buckets.forEach(b => {{
                const hasLogs = b.total > 0;
                // 若有 log 則依比例高度 (最矮 15%)，若無 log 則給 4px 灰色極細底線佔位
                const heightStyle = hasLogs ? `height: ${{Math.max(15, Math.round((b.total / maxTotal) * 100))}}%;` : 'height: 4px;';
                let errCount = b.error || 0;
                let warnCount = b.warn || 0;
                let infoCount = b.info || 0;
                // 容錯防禦：若有 log 但分類加總為 0，預設將 info 設為 total，避免子 div 高度 0% 隱形
                if (hasLogs && (errCount + warnCount + infoCount === 0)) {{
                    infoCount = b.total;
                }}
                const errPct = hasLogs ? (errCount / b.total) * 100 : 0;
                const warnPct = hasLogs ? (warnCount / b.total) * 100 : 0;
                const infoPct = hasLogs ? (infoCount / b.total) * 100 : 0;
                const timeLabel = formatTimeLabel(b.time);

                barsHtml += `
                    <div class="flex-1 min-w-[6px] max-w-[28px] h-full flex flex-col justify-end group relative cursor-pointer">
                        <div class="w-full rounded-t-xs overflow-hidden flex flex-col-reverse transition-all group-hover:brightness-90" style="${{heightStyle}}">
                            ${{hasLogs ? `
                                <div style="height: ${{errPct}}%;" class="bg-rose-500 w-full"></div>
                                <div style="height: ${{warnPct}}%;" class="bg-amber-400 w-full"></div>
                                <div style="height: ${{infoPct}}%;" class="bg-emerald-500 w-full"></div>
                            ` : `
                                <div class="bg-slate-200 hover:bg-slate-300 w-full h-full"></div>
                            `}}
                        </div>
                        <!-- Tooltip 浮動提示 -->
                        <div class="hidden group-hover:block absolute bottom-full mb-1.5 left-1/2 -translate-x-1/2 z-30 bg-slate-900 text-white text-[11px] font-mono py-1.5 px-2.5 rounded-lg shadow-xl whitespace-nowrap pointer-events-none">
                            <div class="font-bold border-b border-slate-700 pb-1 mb-1 text-slate-200">${{timeLabel}}</div>
                            <div>總筆數: <b class="text-white">${{b.total}}</b> 筆</div>
                            ${{errCount > 0 ? `<div class="text-rose-400">🔴 ERROR: ${{errCount}}</div>` : ''}}
                            ${{warnCount > 0 ? `<div class="text-amber-300">🟡 WARN: ${{warnCount}}</div>` : ''}}
                            ${{infoCount > 0 ? `<div class="text-emerald-300">🟢 INFO: ${{infoCount}}</div>` : ''}}
                        </div>
                    </div>
                `;
            }});

            container.innerHTML = barsHtml;
            const firstTime = formatTimeLabel(buckets[0].time);
            const midIndex = Math.floor(buckets.length / 2);
            const midTime = formatTimeLabel(buckets[midIndex].time);
            const lastTime = formatTimeLabel(buckets[buckets.length - 1].time);
            axis.innerHTML = `<span>${{firstTime}}</span><span class="text-slate-500 font-semibold">${{midTime}}</span><span>${{lastTime}}</span>`;
        }}

        function renderLogItems(logs) {{
            const container = document.getElementById('logsContainer');
            if (!logs || logs.length === 0) {{
                container.innerHTML = `
                    <div class="bg-white border border-slate-200 rounded-2xl p-12 text-center text-slate-400">
                        <span class="text-4xl block mb-2">🍃</span>
                        <p class="text-sm font-semibold text-slate-700">查無符合條件的日誌資料</p>
                        <p class="text-xs text-slate-400 mt-1">請嘗試放寬時間範圍、變更日誌等級或調整關鍵字搜尋條件</p>
                    </div>
                `;
                return;
            }}

            let html = '';
            logs.forEach((log, index) => {{
                const ts = log['@timestamp'] || log['timestamp'] || '-';
                const lvl = (log['level'] || log['log.level'] || (log['log'] && log['log']['level']) || 'INFO').toUpperCase();
                const sname = log['service_name'] || log['service.name'] || log['service']?.name || 'global';
                const msg = log['message'] || log['error_message'] || JSON.stringify(log);
                const loc = log['error_location'] || (log['caller'] ? `${{log['caller']['file']}}:${{log['caller']['line']}}` : '');

                let badgeColor = 'bg-slate-100 text-slate-700 border-slate-200';
                let dotColor = 'bg-slate-400';
                if (lvl === 'ERROR' || lvl === 'CRITICAL' || lvl === 'FATAL') {{
                    badgeColor = 'bg-rose-50 text-rose-700 border-rose-200';
                    dotColor = 'bg-rose-500';
                }} else if (lvl === 'WARN' || lvl === 'WARNING') {{
                    badgeColor = 'bg-amber-50 text-amber-700 border-amber-200';
                    dotColor = 'bg-amber-500';
                }} else if (lvl === 'INFO') {{
                    badgeColor = 'bg-emerald-50 text-emerald-700 border-emerald-200';
                    dotColor = 'bg-emerald-500';
                }}

                const escapedLoc = loc ? `<span class="px-2 py-0.5 bg-slate-100 text-slate-600 rounded text-[11px] font-mono border border-slate-200">${{escapeHtml(loc)}}</span>` : '';

                // 提取並顯示運行環境 (Environment) 徽章
                const env = log['environment'] || (log['service'] && log['service']['environment']) || log['service.environment'] || log['env'] || '';
                let envBadge = '';
                if (env) {{
                    let envColor = 'bg-slate-100 text-slate-600 border-slate-200';
                    const envLower = String(env).toLowerCase();
                    if (envLower.includes('prod') || envLower.includes('formal')) {{
                        envColor = 'bg-purple-50 text-purple-700 border-purple-200 font-bold';
                    }} else if (envLower.includes('dev')) {{
                        envColor = 'bg-amber-50 text-amber-700 border-amber-200 font-bold';
                    }} else if (envLower.includes('test') || envLower.includes('stage')) {{
                        envColor = 'bg-cyan-50 text-cyan-700 border-cyan-200 font-bold';
                    }} else if (envLower.includes('local')) {{
                        envColor = 'bg-emerald-50 text-emerald-700 border-emerald-200';
                    }}
                    envBadge = `<span class="px-2 py-0.5 rounded-full text-[11px] font-mono border ${{envColor}}">🌐 ${{escapeHtml(env)}}</span>`;
                }}

                html += `
                    <div class="bg-white border border-slate-200 hover:border-slate-300 rounded-xl p-4 shadow-2xs transition flex flex-col gap-2">
                        <div class="flex items-center justify-between flex-wrap gap-2 text-xs">
                            <div class="flex items-center gap-2 flex-wrap">
                                <span class="w-2 h-2 rounded-full ${{dotColor}}"></span>
                                <span class="font-mono text-slate-500 font-semibold">${{escapeHtml(ts)}}</span>
                                <span class="px-2 py-0.5 rounded-full font-bold font-mono text-[11px] border ${{badgeColor}}">${{lvl}}</span>
                                <span class="px-2 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 font-mono text-[11px] rounded font-bold">${{escapeHtml(Array.isArray(sname) ? sname[0] : sname)}}</span>
                                ${{envBadge}}
                                ${{escapedLoc}}
                            </div>
                            <div>
                                <button onclick="showDetail(${{index}})" class="text-xs font-semibold text-indigo-600 hover:text-indigo-800 bg-indigo-50 hover:bg-indigo-100 px-2.5 py-1 rounded-lg transition">
                                    展開完整日誌 🔍
                                </button>
                            </div>
                        </div>
                        <div class="bg-slate-900 text-slate-100 p-3 rounded-lg text-xs font-mono whitespace-pre-wrap break-all leading-relaxed max-h-40 overflow-y-auto">
                            ${{escapeHtml(msg)}}
                        </div>
                    </div>
                `;
            }});

            container.innerHTML = html;
        }}

        function showDetail(index) {{
            const log = currentLogs[index];
            if (!log) return;
            document.getElementById('modalJsonContent').innerText = JSON.stringify(log, null, 2);
            document.getElementById('logDetailModal').classList.remove('hidden');
        }}

        function closeDetailModal() {{
            document.getElementById('logDetailModal').classList.add('hidden');
        }}

        function copyModalJson() {{
            const text = document.getElementById('modalJsonContent').innerText;
            navigator.clipboard.writeText(text).then(() => {{
                alert('已成功將日誌 JSON 複製至剪貼簿！');
            }});
        }}

        function escapeHtml(str) {{
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }}

        // 頁面載入後自動解析 URL 參數、填入專案、環境與時間範圍選單並發動查詢
        window.addEventListener('DOMContentLoaded', async () => {{
            const urlParams = new URLSearchParams(window.location.search);
            const targetSvc = urlParams.get('service') || urlParams.get('service_name') || '{default_service}';
            const targetEnv = urlParams.get('environment') || urlParams.get('env') || '{default_env}';
            const targetLevel = urlParams.get('level');
            const targetTimeRange = urlParams.get('time_range') || urlParams.get('range') || '{default_time_range}';

            if (targetSvc) {{
                const svcSelect = document.getElementById('serviceSelect');
                if (svcSelect) svcSelect.value = targetSvc;
            }}

            if (targetLevel) {{
                const lvlSelect = document.getElementById('levelSelect');
                if (lvlSelect) lvlSelect.value = targetLevel;
            }}

            if (targetTimeRange) {{
                const trSelect = document.getElementById('timeRangeSelect');
                if (trSelect) {{
                    trSelect.value = targetTimeRange;
                    if (typeof toggleCustomTime === 'function') {{
                        toggleCustomTime();
                    }}
                }}
            }}

            // 載入該專案的可用環境並自動選取 targetEnv
            await loadEnvironments(targetEnv);

            if (targetEnv) {{
                const envSelect = document.getElementById('envSelect');
                if (envSelect) envSelect.value = targetEnv;
            }}

            // 立即發動探測查詢！
            queryLogs();
        }});
    </script>
</body>
</html>'''

