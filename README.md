# SRE Sentinel AI Agent 運維與獨立專案部署指南

本專案為 **中央 SRE 智能日誌分析與自動自癒系統（Central SRE Sentinel Agent）**。  
支援同時監控多個微服務專案（如 `isocial-rag-backend`、`family-tree-backend` 等），具備 **全域關聯分析、Master Markdown 報表、防洗版 GitLab Issue 同步、一鍵 ChatOps 自癒修復與自適應經驗知識庫** 等核心能力。

---

## 目錄結構說明

搬移至獨立專案目錄（例如 `sre-sentinel-agent/`）後，建議保持以下標準結構：

```text
elk_log_analyzer_agent/
├── projects.json              # 【專案清單】多微服務映射 (service_name ➔ repo/branch)
├── main.py                    # 【主入口】排程日誌分析 & ChatOps FastAPI 服務
├── requirements.txt           # Python 依賴清單
├── .env.example               # 環境變數設定範本
├── .env                       # 實體環境變數配置 (包含帳密與 API 金鑰)
├── .gitignore                 # Git 忽略清單 (忽略 .env, repos/, 快取等)
├── README.md                  # 專案說明與運維手冊
│
├── skills/                    # 🌟 【SRE 伺服器原生技能庫 (LLM Tools)】
│   ├── __init__.py            # 技能宣告表 (fetch_code_snippet, read_file_range, search_symbol)
│   ├── code_tools.py          # 代碼精準前後 20 行切片與行號標註
│   └── search_tools.py        # 跨檔案符號定義搜尋
│
├── core/                      # ⚙️ 【核心系統模組】
│   ├── repo_manager.py        # 各專案 main 分支動態鏡像同步 (git clone / fetch)
│   └── report_generator.py    # 多專案分層報表與全域儀表板 (index.md) 維護
│
├── agents/                    # 🤖 【SRE 執行 Agent 團隊】
│   └── remediation_agent.py   # 自癒修復 Agent (CI 遠端下載跑修碼、測試、提 MR)
│
├── repos/                     # 【各專案 main 代碼鏡像】(執行時動態產生，已 Git 忽略)
│   ├── socialwork2-rag-backend/
│   └── family-tree-backend/
│
├── reports/                   # 📄 【多專案診斷報表庫】
│   ├── index.md               # 全域健康總覽儀表板
│   └── socialwork2-rag-backend/
│       ├── index.md           # 專案歷史報表索引
│       └── report_YYYYMMDD.md # 每日全域診斷報表
│
├── tools/                     # 🛠️ 【輔助與測試工具】
│   ├── trigger_test_logs.py           # 發送模擬 ERROR 日誌至 Logstash
│   ├── rag_stimulator.py              # 本地 RAG 提問與邊界測試流量刺激器
│   └── remote_scheduled_stimulator.py # 遠端真實個案壓測與排程刺激器
│
├── docs/                      # 架構與演進藍圖文件
│   └── ai_debug_agent_roadmap.md
│
└── knowledge_base/            # 自癒案例資料庫 (自學習沉澱經驗，跨專案共享)
    └── cases/                 # 歷次驗證通過的修復案例 JSON
```

---

## 快速遷移步驟（Step-by-Step）

### 步驟 1：建立獨立目錄並複製檔案
將 `elk_log_analyzer_agent/` 內的檔案完整複製到獨立專案目錄中。

### 步驟 2：建立 Python 虛擬環境與安裝依賴
```bash
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

pip install -r requirements.txt
```

#### `requirements.txt` 核心清單：
```text
elasticsearch>=8.0.0
httpx>=0.24.0
python-dotenv>=1.0.0
pydantic>=2.0.0
fastapi>=0.100.0
uvicorn>=0.22.0
openai>=1.0.0
pytest>=7.0.0
```

### 步驟 3：配置環境變數 (`.env`)
複製 `.env.example` 為 `.env` 並填入對應設定：

```ini
# =============================================================================
# 1. Elasticsearch 日誌伺服器連線
# =============================================================================
ELASTICSEARCH_URL=http://192.168.10.135:9200
ELASTICSEARCH_USER=elastic
ELASTICSEARCH_PASSWORD=your_elastic_password
ELASTICSEARCH_INDEX=logs-*

# =============================================================================
# 2. GitLab 專案與 API 權限設定 (支援預設專案與多專案映射)
# =============================================================================
GITLAB_URL=http://192.168.20.112:8929
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_PROJECT_ID=15
GITLAB_TRIGGER_TOKEN=your_pipeline_trigger_token

# =============================================================================
# 3. 國網 NCHC / LLM 模型設定 (用於全域診斷與修復計畫生成)
# =============================================================================
NCHCCLOUD_API_KEY=your_nchc_api_key
NCHCCLOUD_API_URL=https://portal.genai.nchc.org.tw/api/v1
MODEL_NAME=gemma-4-26B-A4B-it

# =============================================================================
# 4. ChatOps Webhook 伺服器監聽設定
# =============================================================================
SERVER_PORT=8300
SERVER_HOST=0.0.0.0
CHATOPS_BASE_URL=http://localhost:8300
```

---

## 多專案接入方式（Multi-Project Observability）

### 1. 業務專案端：日誌帶上專屬 `service_name`
各業務專案（如 `isocial-rag-backend`、`family-tree-backend`）在向 Logstash / ELK 送出錯誤日誌時，Payload 需包含以下結構化欄位：

```json
{
  "timestamp": "2026-09-14T14:30:00+08:00",
  "service_name": "socialwork2-rag-backend",
  "environment": "production",
  "level": "ERROR",
  "error_location": "embedding_client.py:104",
  "error_type": "structured-visit_LLM_Generation_Failed",
  "error_message": "NCHC 503 Service Unavailable",
  "error_stack": "Traceback (most recent call last)...",
  "inputs": { ... }
}
```

### 2. SRE Agent 端：多專案對應映射
在 `main.py` 或設定檔中，可依據 `service_name` 自動將診斷結果與 Issue 分派到各專案的 GitLab：

| service_name | GitLab 專案名稱 | GitLab Project ID |
| :--- | :--- | :---: |
| `socialwork2-rag-backend` | `isocial-rag-demo` | `15` |
| `family-tree-backend` | `family-tree` | `18` |

---

## CI/CD 一鍵自動自癒設定（主專案端）

被監控的業務主專案（如 `isocial-rag-demo`）**不需要**在 Repo 內保留任何修復腳本，只要在 `.gitlab-ci.yml` 加上自癒 Trigger Job：

```yaml
# 業務專案 .gitlab-ci.yml 範例
stages:
  - test
  - heal

auto-heal:
  stage: heal
  image: python:3.11
  rules:
    - if: '$CI_PIPELINE_SOURCE == "trigger"'
  script:
    # 1. 從 SRE 中央專案動態抓取最新版 remediation_agent.py
    - curl -s http://192.168.20.112:8929/root/sre-sentinel-agent/-/raw/main/agents/remediation_agent.py -o remediation_agent.py
    # 2. 執行自癒修復 (解析 Issue 診斷計畫 -> 套用修復 -> 跑測試 -> 自動開分支發起 MR)
    - python remediation_agent.py --issue-iid $ISSUE_IID
```

---

## 常用操作指令

### 1. 執行日誌掃描與全域分析（CLI 模式）
撈取過去 24 小時內的所有錯誤日誌，自動依專案同步 main 代碼鏡像，調用原生技能進行代碼級診斷，產出多專案分層報表並更新 GitLab Issue：
```bash
python main.py --hours 24
```

### 2. 啟動 SRE Sentinel 全域監控儀表板與 ChatOps（Web 伺服器模式）
啟動背景 Web 服務，預設監聽 `8300` Port：
```bash
python main.py --server
```
或手動指定其他連接埠：
```bash
python main.py --server --port 8300
```
* **🌐 全域微服務監控看板**：瀏覽器打開 `http://localhost:8300/`
  - 即時查看所有受監控微服務專案的健康狀態矩陣（🔴 CRITICAL / 🟡 WARNING / 🟢 HEALTHY）
  - 線上一鍵查閱每日代碼切片診斷報告（高質感深色排版 + 語法高亮）
  - 提供「⚡ 立即掃描日誌」一鍵即時巡檢按鈕
* **🤖 ChatOps 自癒觸發端點**：`http://localhost:8300/heal?issue_iid=<IID>&project_id=15`

### 3. 單獨測試自癒修復（本地除錯模式）
模擬 CI Runner 環境，手動針對特定 GitLab Issue 執行修復與測試：
```bash
python agents/remediation_agent.py --issue-iid 123
```

---

## 自學習經驗知識庫（Knowledge Base）機制

1. **案例累積**：當 `agents/remediation_agent.py` 成功修復代碼並通過 `pytest` 測試後，會自動在 `knowledge_base/cases/` 產生結構化案例檔案（如 `case_20260909_01.json`）。
2. **經驗反哺**：下次 `main.py` 遇到相同或類似的錯誤類型（如 503 重試策略、Pydantic 欄位驗證等）時，會將歷史成功修復案例作為 Few-shot 參考提供給 LLM，大幅提升一次修復成功的準確率！
