# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple


def normalize_error_signature(text: str) -> str:
    """
    高精度保守正規化：
    針對動態跳動數值（如耗時毫秒、IP、UUID、數字ID）進行標準化遮罩，
    同時嚴格保留英文單詞語意（如 Slow query found, AES Decryption Failed 等），
    避免將相同根本原因切碎成多個獨立指紋。
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


def compute_fingerprint(service_name: str, location: str, error_type: str) -> str:
    """
    根據微服務名稱、出錯檔案與行號、錯誤型態計算出唯一的 12 碼特徵指紋
    具備極致防呆，容忍 None 或任何資料型態
    """
    sname = str(service_name or "unknown_service").strip()
    loc = str(location or "unknown:0").strip()
    etype = str(error_type or "unknown_type").strip()
    norm_etype = normalize_error_signature(etype)
    raw_str = f"{sname}::{loc}::{norm_etype}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:12]


class IssueLifecycleTracker:
    """
    異常生命週期追蹤器：
    負責管理 reports/<service_name>/issue_state.json，
    追蹤每個錯誤指紋的首發日期、連續出現天數、歷史累積次數、以及前次 AI 診斷快取。
    """

    def __init__(self, base_dir: str = "reports"):
        self.base_dir = os.path.abspath(base_dir)

    def _get_state_file_path(self, service_name: str) -> str:
        proj_dir = os.path.join(self.base_dir, service_name)
        os.makedirs(proj_dir, exist_ok=True)
        return os.path.join(proj_dir, "issue_state.json")

    def migrate_and_normalize_state(self, service_name: str, state: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """
        自動檢查並遷移歷史未正規化的指紋與資料，
        將舊版切碎之項目（如各種浮點數毫秒的 Slow query）合併至單一正規化指紋。
        """
        issues = state.get("issues", {})
        if not issues:
            return state, False

        modified = False
        merged_issues = {}

        for fp, item in issues.items():
            loc = str(item.get("error_location") or "unknown:0").strip()
            raw_etype = str(item.get("error_type") or "unknown_type").strip()
            norm_etype = normalize_error_signature(raw_etype)
            new_fp = compute_fingerprint(service_name, loc, norm_etype)

            if new_fp != fp or norm_etype != raw_etype:
                modified = True

            if new_fp in merged_issues:
                # 進行重複項目合併
                modified = True
                target = merged_issues[new_fp]
                target["total_occurrences"] = target.get("total_occurrences", 0) + item.get("total_occurrences", 0)
                target["today_count"] = target.get("today_count", 0) + item.get("today_count", 0)
                target["yesterday_count"] = target.get("yesterday_count", 0) + item.get("yesterday_count", 0)

                # 合併每日歷史 (daily_history)
                target_hist = target.setdefault("daily_history", {})
                for d, cnt in item.get("daily_history", {}).items():
                    target_hist[d] = target_hist.get(d, 0) + cnt

                # 首次發現日取最早
                d1 = target.get("first_seen_date", "")
                d2 = item.get("first_seen_date", "")
                if d2 and (not d1 or d2 < d1):
                    target["first_seen_date"] = d2

                # 最後發現日取最新
                ld1 = target.get("last_seen_date", "")
                ld2 = item.get("last_seen_date", "")
                if ld2 and (not ld1 or ld2 > ld1):
                    target["last_seen_date"] = ld2

                # 保留已知標記
                if item.get("is_acknowledged") or item.get("status") == "acknowledged":
                    target["is_acknowledged"] = True
                    target["status"] = "acknowledged"
                    if item.get("acknowledge_note"):
                        target["acknowledge_note"] = item.get("acknowledge_note")

                # 保留根因分析快取
                if not target.get("cached_analysis") and item.get("cached_analysis"):
                    target["cached_analysis"] = item.get("cached_analysis")
            else:
                new_item = dict(item)
                new_item["fingerprint"] = new_fp
                new_item["error_type"] = norm_etype
                merged_issues[new_fp] = new_item

        state["issues"] = merged_issues
        return state, modified

    def load_state(self, service_name: str) -> Dict[str, Any]:
        path = self._get_state_file_path(service_name)
        if not os.path.exists(path):
            return {"service_name": service_name, "issues": {}, "last_updated": ""}
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            
            # 自動執行指紋正規化遷移與清理
            state, modified = self.migrate_and_normalize_state(service_name, state)
            if modified:
                self.save_state(service_name, state)
            return state
        except Exception as e:
            print(f"⚠️ [IssueTracker] 讀取狀態檔失敗 ({service_name}): {e}")
            return {"service_name": service_name, "issues": {}, "last_updated": ""}

    def save_state(self, service_name: str, state: Dict[str, Any]) -> bool:
        path = self._get_state_file_path(service_name)
        try:
            state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"⚠️ [IssueTracker] 儲存狀態檔失敗 ({service_name}): {e}")
            return False

    def update_lifecycle(
        self,
        service_name: str,
        aggregated_errors: List[Dict[str, Any]],
        today_date_str: str = None
    ) -> Dict[str, Any]:
        """
        核心更新邏輯：
        比對歷史狀態，更新每一個 error 的指紋、連續天數、次數與生命週期狀態，
        並回傳整體的生命週期統計摘要 (lifecycle_summary)。
        """
        today_str = today_date_str or datetime.now().strftime("%Y-%m-%d")
        yesterday_str = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

        state = self.load_state(service_name)
        saved_issues = state.get("issues", {})

        current_fps = set()
        new_issues_count = 0
        ongoing_issues_count = 0
        max_consecutive_days = 0
        total_today_errors = 0
        total_yesterday_errors = 0

        for err in aggregated_errors:
            loc = str(err.get("error_location") or "unknown:0").strip()
            etype = str(err.get("error_type") or "unknown_type").strip()
            err["error_location"] = loc
            err["error_type"] = etype
            fp = compute_fingerprint(service_name, loc, etype)
            current_fps.add(fp)

            count_today = err.get("count", 1)
            total_today_errors += count_today

            # 檢查歷史紀錄
            if fp in saved_issues:
                item = saved_issues[fp]
                last_seen = item.get("last_seen_date", "")
                prev_consecutive = item.get("consecutive_days", 1)

                if last_seen == today_str:
                    # 當天內多次掃描，天數不重複加，但更新次數
                    consecutive_days = prev_consecutive
                    today_occurrences = count_today
                elif last_seen == yesterday_str:
                    # 連續第 N 天 (昨天有發生，今天又發生)
                    consecutive_days = prev_consecutive + 1
                    today_occurrences = count_today
                else:
                    # 間隔數天後復發
                    consecutive_days = 1
                    today_occurrences = count_today

                item["yesterday_count"] = item.get("today_count", 0) if last_seen != today_str else item.get("yesterday_count", 0)
                item["today_count"] = today_occurrences
                item["total_occurrences"] = item.get("total_occurrences", 0) + (count_today if last_seen != today_str else 0)
                item["last_seen_date"] = today_str
                item["consecutive_days"] = consecutive_days
                item.setdefault("daily_history", {})[today_str] = today_occurrences

                # 若已被工程師標記為已知待處理，維持 acknowledged 狀態
                if item.get("status") == "acknowledged":
                    item["is_acknowledged"] = True
                else:
                    item["status"] = "ongoing" if consecutive_days > 1 else "new"
                    item["is_acknowledged"] = False

                if item.get("status") == "acknowledged":
                    pass # 不計入需緊急通報的 new 或 ongoing
                elif consecutive_days > 1:
                    ongoing_issues_count += 1
                else:
                    new_issues_count += 1

                total_yesterday_errors += item.get("yesterday_count", 0)
                max_consecutive_days = max(max_consecutive_days, consecutive_days)

            else:
                # 全新指紋
                consecutive_days = 1
                saved_issues[fp] = {
                    "fingerprint": fp,
                    "service_name": service_name,
                    "error_location": loc,
                    "error_type": etype,
                    "first_seen_date": today_str,
                    "last_seen_date": today_str,
                    "consecutive_days": 1,
                    "today_count": count_today,
                    "yesterday_count": 0,
                    "total_occurrences": count_today,
                    "status": "new",
                    "is_acknowledged": False,
                    "cached_analysis": None,
                    "daily_history": {today_str: count_today}
                }
                new_issues_count += 1
                max_consecutive_days = max(max_consecutive_days, 1)

            # 將生命週期資訊直接灌入該項 error 物件，供後續報告與分析使用
            target_item = saved_issues[fp]
            err["fingerprint"] = fp
            err["status"] = target_item["status"]
            err["is_new"] = (target_item["status"] == "new")
            err["is_ongoing"] = (target_item["status"] == "ongoing")
            err["is_acknowledged"] = (target_item.get("status") == "acknowledged")
            err["acknowledge_note"] = target_item.get("acknowledge_note", "")
            err["consecutive_days"] = target_item["consecutive_days"]
            err["total_occurrences"] = target_item["total_occurrences"]
            err["cached_analysis"] = target_item.get("cached_analysis")

        # 標記那些今天沒出現的歷史問題 (可能已被修復)
        for fp, item in saved_issues.items():
            if fp not in current_fps and item.get("status") != "resolved":
                if item.get("last_seen_date") != today_str:
                    item["status"] = "resolved"

        state["issues"] = saved_issues
        self.save_state(service_name, state)

        # 計算惡化指標 (例如今日錯誤量為昨日 3 倍以上)
        is_escalated = False
        if total_yesterday_errors > 0 and total_today_errors >= (total_yesterday_errors * 3):
            is_escalated = True

        summary = {
            "service_name": service_name,
            "total_issues": len(aggregated_errors),
            "new_count": new_issues_count,
            "chronic_count": ongoing_issues_count,
            "max_consecutive_days": max_consecutive_days,
            "total_today_errors": total_today_errors,
            "total_yesterday_errors": total_yesterday_errors,
            "is_escalated": is_escalated
        }
        return summary

    def acknowledge_issue(self, service_name: str, fingerprint: str, note: str = "") -> bool:
        """將特定異常指紋標記為已知待排期 (acknowledged)，啟動告警靜音"""
        state = self.load_state(service_name)
        saved = state.get("issues", {})
        if fingerprint in saved:
            saved[fingerprint]["status"] = "acknowledged"
            saved[fingerprint]["is_acknowledged"] = True
            saved[fingerprint]["acknowledge_note"] = note or "工程師已確認知情，暫緩處理"
            saved[fingerprint]["acknowledged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return self.save_state(service_name, state)
        return False

    def unacknowledge_issue(self, service_name: str, fingerprint: str) -> bool:
        """取消已知標記，恢復正常警報與追蹤"""
        state = self.load_state(service_name)
        saved = state.get("issues", {})
        if fingerprint in saved:
            consecutive = saved[fingerprint].get("consecutive_days", 1)
            saved[fingerprint]["status"] = "ongoing" if consecutive > 1 else "new"
            saved[fingerprint]["is_acknowledged"] = False
            saved[fingerprint].pop("acknowledge_note", None)
            saved[fingerprint].pop("acknowledged_at", None)
            return self.save_state(service_name, state)
        return False

    def update_cached_analysis(self, service_name: str, ai_analysis: Dict[str, Any]) -> None:
        """儲存或更新 LLM 生成的修復建議快取"""
        state = self.load_state(service_name)
        saved = state.get("issues", {})
        # 為當前所有開啟的問題寫入快取
        for fp, item in saved.items():
            if item.get("status") in ("new", "ongoing", "acknowledged"):
                item["cached_analysis"] = {
                    "severity": ai_analysis.get("severity"),
                    "title": ai_analysis.get("title"),
                    "root_cause_summary": ai_analysis.get("root_cause_summary") or ai_analysis.get("correlation_analysis"),
                    "coordinated_fix_plan": ai_analysis.get("coordinated_fix_plan")
                }
        self.save_state(service_name, state)

    def should_send_email_alert(
        self,
        service_name: str,
        lifecycle_summary: Dict[str, Any],
        has_prod_error: bool = False
    ) -> Tuple[bool, str]:
        """
        Email 智慧降噪決策：
        1. 若包含新問題 (new_count > 0) ➡️ 發送 (🚨 發現全新異常)
        2. 若錯誤量暴增 3 倍以上 (is_escalated) ➡️ 發送 (⚠️ 異常急劇惡化)
        3. 若錯誤出現在正式環境 (has_prod_error) 且為首次或惡化 ➡️ 發送
        4. 其餘均為已知的持續頑疾 (ongoing) 或已標記已知 (acknowledged) ➡️ 自動靜音 (🔕 降噪略過)
        """
        new_cnt = lifecycle_summary.get("new_count", 0)
        is_esc = lifecycle_summary.get("is_escalated", False)
        tot_today = lifecycle_summary.get("total_today_errors", 0)
        tot_yest = lifecycle_summary.get("total_yesterday_errors", 0)

        # 1. 有全新問題
        if new_cnt > 0:
            return True, f"檢測到 {new_cnt} 個全新異常問題，需立即通知負責人"

        # 2. 錯誤急劇惡化
        if is_esc:
            return True, f"錯誤頻率急劇惡化（今日 {tot_today} 次，昨日 {tot_yest} 次，增幅超過 3 倍）"

        # 3. 正式環境錯誤，如果是新發生或次數增加
        if has_prod_error and tot_today > tot_yest and tot_yest > 0:
            return True, f"正式環境 (production) 異常次數持續攀升（今日 {tot_today} 次，昨日 {tot_yest} 次）"

        # 4. 全部為已知舊問題或已標記已知
        return False, f"所有 {lifecycle_summary.get('chronic_count', 0)} 個問題均為已追蹤之頑疾或已知技術債，自動啟動 Email 靜音保護"


# 全域單例
tracker = IssueLifecycleTracker()
