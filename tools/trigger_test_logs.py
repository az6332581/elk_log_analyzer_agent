import os
import sys
import io
import json
import httpx
import uuid
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 強制設定標準輸出為 UTF-8 編碼，防止 Windows 下 cp950 崩潰
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# 載入環境變數（支援在根目錄或 tools/ 目錄下執行）
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))
load_dotenv()

LOGSTASH_URL = os.getenv("LOGSTASH_URL", "http://192.168.10.135:5055")
LOGSTASH_USER = os.getenv("LOGSTASH_USER")
LOGSTASH_PASSWORD = os.getenv("LOGSTASH_PASSWORD")
APP_ENV = os.getenv("APP_ENV", "production")

def send_test_log(error_type, error_msg, file_line, offset_minutes=0):
    """
    發送一筆測試的結構化錯誤日誌到 Logstash。
    :param offset_minutes: 可以設定時間偏移，例如 -10 代表 10 分鐘前的日誌
    """
    # 產生帶有偏移的時間
    log_time = datetime.now() - timedelta(minutes=offset_minutes)
    
    payload = {
        "timestamp": log_time.astimezone().isoformat(),
        "generation_id": f"failed-{error_type}-{uuid.uuid4().hex[:8]}",
        "service_name": "socialwork2-rag-backend-test",
        "environment": APP_ENV,
        "level": "ERROR",
        "error_location": file_line,
        "role": "dmh",
        "inputs": {
            "role": "dmh",
            "county": "Taipei",
            "transcript_len": 80,
            "transcript": "這是一筆測試用的訪視對話紀錄。AI Agent 每三小時分析時應該能順利撈到這筆並完成分析。"
        },
        "error_type": error_type,
        "error_message": error_msg,
        "error_stack": f"Traceback (most recent call last):\n  File \"app/main.py\", line 42, in test_run\n    raise Exception(\"{error_msg}\")\nException: {error_msg}",
        "llm_raw_output": "{\"error\": \"mock raw output for testing\"}"
    }
    
    print(f"正在發送測試日誌 -> [{error_type}] 到 Logstash 端點 {LOGSTASH_URL} ...")
    try:
        auth = (LOGSTASH_USER, LOGSTASH_PASSWORD) if LOGSTASH_USER and LOGSTASH_PASSWORD else None
        resp = httpx.post(LOGSTASH_URL, json=payload, auth=auth, timeout=5.0)
        if resp.status_code < 400:
            print(f"✅ 發送成功！HTTP {resp.status_code}")
        else:
            print(f"⚠️ 發送失敗: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ 無法連線至 Logstash 發送日誌: {e}")

if __name__ == "__main__":
    print("=== KMS/RAG 錯誤日誌測試產生器 ===")
    
    # 隨機產生兩筆不同時間段的錯誤日誌
    # 1. 10 分鐘前發生的資料庫超時
    send_test_log(
        error_type="database_connection_timeout",
        error_msg="Connection pool max limit (50) reached. Cannot acquire new connection.",
        file_line="database/db_pool.py:88",
        offset_minutes=10
    )
    
    # 2. 30 分鐘前發生的 LLM 請求被 Rate limit
    send_test_log(
        error_type="llm_rate_limit_exceeded",
        error_msg="429 Too Many Requests: NCHC API rate limit exceeded (60 RPM).",
        file_line="app/services/llm_client.py:204",
        offset_minutes=30
    )
    
    print("\n測試日誌發送程序執行完畢。請確認 ELK 收件 index 是否已有資料。")
