# [SRE-Weekly] socialwork2-rag-backend 週度營運穩定度與架構綜整報告 (2026_W42)

> **微服務專案**：`socialwork2-rag-backend`  
> **統計週期**：過去 7 天（168 小時） ｜ **週次標記**：`2026_W42`  
> **整體穩定度評級**：**Warning** ｜ **7 天累計異常數**：`2` 次 ｜ **獨立問題項目**：`2` 個  
> **報告生成時間**：2026-10-14 17:15:05

---

## 📈 1. 本週穩定度趨勢與總結 (Weekly Executive Summary)

本週系統整體穩定度呈現「低頻但高風險」的特徵。雖然 7 天內僅發生 2 次異常日誌，且集中於 2026-10-14 單日，但異常類型高度集中於外部依賴服務（NCHC Embedding API）的流量控制限制。從歷史工單紀錄（10-03 至 10-14）來看，系統長期處於高嚴重度（High Severity）的維運狀態，顯示系統對外部 API 的依賴缺乏韌性緩衝，一旦觸發 Rate Limit，將直接導致 RAG 檢索鏈路中斷。

### 📅 過去 7 天每日異常走勢
| 日期 | 錯誤發生次數 |
| :--- | :---: |
| `2026-10-14` | **2** 次 |

### 🌐 運行環境分佈 (Environments)
- **production**: 2 次錯誤

---

## 🦠 2. 本週重複性頑疾與技術債追蹤 (Chronic & Recurring Issues)

### ⚠️ 頑疾 1：`app/services/embedding_client.py:30`
* **問題描述**：外部 Embedding API (NCHC) 觸發 HTTP 429 Too Many Requests 錯誤。根本原因在於當前 RAG 檢索請求量瞬間峰值超過了第三方服務的 QPS 限制，且代碼層級缺乏有效的流量整形（Traffic Shaping）或指數退避重試機制。
* **潛在風險與衝擊**：直接導致 QA Retrieval 階段失敗，造成使用者端 RAG 問答功能無法正常運作，影響系統核心業務可用性。


---

## 🛠️ 3. 下週專案架構調優建議與行動清單 (Actionable SRE Roadmap)

### 📋 建議落實清單 (Checklist)
- [ ] 1. 實施流量控制與韌性機制：在 embedding_client.py 中引入帶有指數退避（Exponential Backoff）與抖動（Jitter）的重試機制，並考慮加入本地端 Rate Limiter 以防止對外部 API 的過度衝擊。
- [ ] 2. 引入 Embedding 結果快取層：針對高頻重複的 Query 建立 Redis 快取機制，減少對 NCHC API 的重複呼叫，從根本上降低 QPS 壓力。
- [ ] 3. 優化監控告警閾值：建立針對外部 API 回傳 429 狀態碼的專屬監控指標，當 429 錯誤率在 5 分鐘窗口內超過特定閾值時，立即觸發 P1 級別告警，而非僅依賴日誌聚合。

---
*本週報由 SRE AI Sentinel 智慧分析大腦自動彙整產出，專門供 `socialwork2-rag-backend` 專案團隊維護系統穩定度使用。*
