# 🛡️ SRE Sentinel AI Agent：微服務智能日誌分析與自動自癒系統

`SRE Sentinel AI Agent` 是一套專為現代分散式微服務架構設計的 **中央智能 SRE 監控、日誌聚合分析、故障生命週期追蹤與協同自癒系統**。  
支援同時監控多個微服務專案（如 `socialwork2-rag-backend`、`family-tree-backend` 等），具備 **全域因果鏈分析、代碼級精準切片、資料庫化異常生命週期（頑疾追蹤/降噪）、響應式 Email 告警與自動開單自癒** 等核心能力。

---

## 🌟 核心功能特色

1. **純資料庫驅動架構（Database-Native）**：
   - 告別散落在磁碟各處的脆弱 `.json` 狀態檔與 `.md` 文字拼接。
   - 採用 **SQLAlchemy 2.0 ORM**，預設搭載輕量高效的 **SQLite (WAL 模式)**，支援未來一行環境變數無痛轉移至 **MySQL / PostgreSQL**。
2. **異常特徵指紋與生命週期追蹤（Issue Lifecycle Tracker）**：
   - **高精度保守正規化**：自動遮罩跳動毫秒、IP、UUID 與流水號，避免同一慢查詢被切碎。
   - **頑疾持續追蹤**：自動統計首發日期、連續出現天數、歷史累計次數與 7 天走勢。
   - **工程師一鍵降噪**：支援「🔕 暫不處理（Acknowledged）」與「↩️ 恢復追蹤」，標記後自動停止發信干擾。
3. **SRE 代碼切片原生技能（Native Code Tools）**：
   - AI 大腦分析日誌時，自動定位 GitLab 倉庫並切取出錯行前後 20 行真實原始碼（具備業務代碼回溯防呆），實現真正代碼級的因果鏈診斷。
4. **極簡沉浸式 Web 控制台與即時探測器**：
   - 頂部 5 分鐘自動刷新指示器（具備使用者互動防打擾保護）。
   - 中文業務名稱為尊的卡片分層排版，歷史報告支援抽屜式折疊隱藏。
   - 內建 **Log Explorer 即時日誌探測器**，可依微服務、環境（production / development）與等級秒級篩選。
5. **現代化響應式 Email 告警與智慧決策**：
   - 支援專案級獨立收件人與開關切換。
   - 智慧降噪閥門：已知頑疾自動靜音，僅在「全新異常」或「今日錯誤暴增 3 倍以上」時推播。
6. **DevSecOps 與自動安全防護（CI/CD & CVE Audit）**：
   - 內建完整的 GitLab CI Pipeline：自動執行 5 大單元測試。
   - 整合 `pip-audit` 與 `Trivy`，每次 push 自動掃描 Python 套件已知 CVE 漏洞與容器安全。

---

## 📂 專案目錄結構

```text
elk_log_analyzer_agent/
├── core/                      # ⚙️ 【核心系統模組】
│   ├── database.py            # SQLAlchemy ORM 資料庫模型 (Projects, Issues, Diagnoses)
│   ├── web_dashboard.py       # Web 控制台 HTML 渲染、卡片指標、即時日誌探測器
│   ├── issue_tracker.py       # 12 碼特徵指紋計算、生命週期追蹤、Email 智慧降噪決策
│   ├── repo_manager.py        # 微服務專案註冊管理、GitLab 分支抓取與代碼同步
│   └── report_generator.py    # 全景診斷報告格式化、7 天趨勢矩陣生成
│
├── data/                      # 🗄️ 【資料庫持久化目錄】(Docker 掛載，已 Git 忽略)
│   └── sre_sentinel.db        # 生產 SQLite 資料庫 (WAL 高並發模式)
│
├── skills/                    # 🧠 【SRE 原生技能庫 (LLM Tools)】
│   ├── __init__.py            # 技能 Function Calling Schema 宣告
│   ├── code_tools.py          # 出錯程式碼精準前後 20 行切片與行號標註
│   └── search_tools.py        # 跨檔案符號定義搜尋
│
├── agents/                    # 🤖 【自主修復 Agent 團隊】
│   └── remediation_agent.py   # 自動協同自癒 Agent (CI 遠端下載跑修碼、測試、提 MR)
│
├── tests/                     # 🧪 【品質保證單元測試套件】
│   └── test_sentinel_core.py  # 5 大核心功能 (指紋/ORM/狀態機/降噪/Web 渲染) 自動測試
│
├── tools/                     # 🛠️ 【壓力測試與模擬工具】
│   ├── trigger_test_logs.py           # 發送模擬 ERROR 日誌至 Logstash
│   ├── rag_stimulator.py              # 本地 RAG 提問與邊界測試流量刺激器
│   └── remote_scheduled_stimulator.py # 遠端真實個案壓測與排程刺激器
│
├── repos/                     # 📦 【各專案 main 代碼鏡像】(執行時動態產生，已 Git 忽略)
├── reports/                   # 🛡️ 【設定自癒與備份目錄】(存放 .projects_backup.json)
├── knowledge_base/            # 📚 【自學習經驗知識庫】(歷次驗證通過的修復案例 JSON)
│
├── main.py                    # 🚀 【系統主入口】FastAPI 伺服器 & 日誌定時巡檢排程
├── requirements.txt           # Python 依賴清單 (鎖定 starlette>=1.0.1 安全版本)
├── docker-compose.yml         # Docker 容器編排檔 (支援 host 網路與目錄掛載)
├── Dockerfile                 # 容器構建檔 (Python 3.11 + 中文字型支援)
├── .gitlab-ci.yml             # GitLab CI 自動化測試與 CVE 安全掃描管線
├── projects.json              # 微服務專案註冊配置檔
├── .env.example               # 環境變數範本檔
└── .env                       # 實體環境變數配置 (包含金鑰與 Token，嚴禁提交)
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
sqlalchemy>=2.0.0
pymysql>=1.1.0
cryptography>=41.0.0
starlette>=1.0.1
pip-audit>=2.7.0
```

### 步驟 3：配置環境變數 (`.env`)
複製 `.env.example` 為 `.env` 並填入對應設定：

```ini
# =============================================================================
# 1. 資料庫持久化 (支援 SQLite WAL 或 MySQL/PostgreSQL)
# =============================================================================
DATABASE_URL=sqlite:///./data/sre_sentinel.db
# MySQL 連線範例:
# DATABASE_URL=mysql+pymysql://root:password@192.168.10.135:3306/sre_sentinel?charset=utf8mb4

# =============================================================================
# 2. Elasticsearch 日誌伺服器連線
# =============================================================================
ES_HOST=http://192.168.10.135:9200
ES_INDEX=logs-*
ES_USER=elastic
ES_PASSWORD=your_elastic_password

# =============================================================================
# 3. 大語言模型 (LLM / 國網 NCHC / Gemini)
# =============================================================================
LLM_ENDPOINT=https://portal.genai.nchc.org.tw/api/v1
LLM_API_KEY=your_llm_api_key
LLM_MODEL=gemma-4-26B-A4B-it
APP_ENV=production

# =============================================================================
# 4. GitLab 專案與 API 權限設定 (支援預設專案與多專案映射)
# =============================================================================
GITLAB_URL=http://192.168.20.112:8929
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_PROJECT_ID=15
GITLAB_TRIGGER_TOKEN=your_pipeline_trigger_token

# =============================================================================
# 5. SMTP 郵件告警通知設定
# =============================================================================
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password
EMAIL_RECEIVER=your_email@gmail.com

# =============================================================================
# 6. 即時巡檢與排程排定
# =============================================================================
SCAN_HOURS=24
SCAN_INTERVAL_MINUTES=60

# =============================================================================
# 7. ChatOps Webhook 伺服器監聽設定
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

### 3. Docker 容器化一鍵啟動（生產推薦）
專案支援 `docker-compose` 快速部署，自動掛載 `data/` 資料庫、`projects.json` 與 `repos/` 快取：
```bash
# 背景編譯並啟動容器
docker compose up -d --build

# 查閱運行日誌
docker compose logs -f sre-agent
```

### 4. 單獨測試自癒修復（本地除錯模式）
模擬 CI Runner 環境，手動針對特定 GitLab Issue 執行修復與測試：
```bash
python agents/remediation_agent.py --issue-iid 123
```

---

## 🧪 品質保證與 DevSecOps 安全防護

本系統嚴格遵循現代工程實踐，內建完整的自動化單元測試與安全稽核工具：

### 1. 執行核心單元測試
涵蓋「12 碼指紋正規化、SQLAlchemy ORM、生命週期狀態機、Email 智慧降噪閥門、Web Dashboard 冒煙渲染」5 大測試：
```bash
# 執行專屬測試（需加上 --ignore=repos 避免掃描外部微服務庫存）
pytest tests/ -v --ignore=repos/
```

### 2. 套件 CVE 漏洞自動稽核
採用 `pip-audit` 掃描目前所有依賴套件是否具有已公開的 CVE 弱點（已鎖定 Starlette >= 1.0.1 阻斷 CVE-2026-48710 漏洞）：
```bash
pip-audit
```

### 3. GitLab CI/CD 自動化管線 (`.gitlab-ci.yml`)
每次推送代碼至 GitLab 時，管線將依序自動執行：
1. **`unit_tests`**：自動運行 5 項核心單元測試，確保核心邏輯不回退。
2. **`security_cve_audit`**：以 `pip-audit` 檢查相依套件安全性。
3. **`trivy_security_scan`**：調用 Aquasecurity Trivy 進行容器映像檔安全分析。

---

## 自學習經驗知識庫（Knowledge Base）機制

1. **案例累積**：當 `agents/remediation_agent.py` 成功修復代碼並通過 `pytest` 測試後，會自動在 `knowledge_base/cases/` 產生結構化案例檔案（如 `case_20260909_01.json`）。
2. **經驗反哺**：下次 `main.py` 遇到相同或類似的錯誤類型（如 503 重試策略、Pydantic 欄位驗證等）時，會將歷史成功修復案例作為 Few-shot 參考提供給 LLM，大幅提升一次修復成功的準確率！
