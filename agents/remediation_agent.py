import os
import sys
import re
import json
import httpx
import subprocess
from openai import OpenAI
from dotenv import load_dotenv

# 優先載入根目錄環境變數（支援在根目錄或 agents/ 目錄下執行）
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))
load_dotenv()

# GitLab 與大模型設定
GITLAB_URL = os.getenv("GITLAB_URL", "http://192.168.20.112:8929")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GITLAB_PROJECT_ID = os.getenv("GITLAB_PROJECT_ID", "15")
ISSUE_IID = os.getenv("ISSUE_IID")  # 由 GitLab CI 帶入

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma-4-26B-A4B-it")

# 建立 OpenAI/Gemma 客戶端
openai_client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_ENDPOINT
)

def find_target_file(target_name: str) -> str:
    """
    在專案目錄下搜尋指定的檔案名稱，返回相對路徑。
    """
    for root, dirs, files in os.walk("."):
        # 排除 .git, .venv, node_modules 等無效目錄
        if any(p in root for p in [".git", ".venv", "node_modules", "__pycache__", ".ruff_cache"]):
            continue
        if target_name in files:
            return os.path.join(root, target_name)
    return None

def fetch_issue_description(iid: str) -> str:
    """
    透過 GitLab API 取得 Issue 內文。
    """
    print(f"🕵️ 正在從 GitLab 撈取 Issue #{iid} 的診斷資訊...")
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    url = f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/issues/{iid}"
    
    resp = httpx.get(url, headers=headers, timeout=10.0)
    if resp.status_code == 200:
        issue_data = resp.json()
        return issue_data.get("description", "")
    else:
        raise RuntimeError(f"無法取得 Issue 詳情: HTTP {resp.status_code} - {resp.text}")

def parse_issue_description(description: str) -> tuple:
    """
    從 Issue Markdown 內文中解析出「出錯檔案」、「出錯行號」與「建議修復方案」。
    支援單一錯誤格式與全景多問題 Master Report 格式。
    """
    # 搜尋 "出錯位置：`qa.py:49`" 或 "`file.py:123`" 格式
    location_match = re.search(r"出錯位置\*\*：`(.+?):(\d+)`", description) or \
                     re.search(r"`([a-zA-Z0-9_\-\.]+?\.py):(\d+)`", description)
    file_name = None
    line_number = None
    if location_match:
        file_name = location_match.group(1).strip()
        line_number = int(location_match.group(2))

    # 搜尋 "跨檔案協同修復計畫" 或 "AI 建議修復方案"
    fix_match = re.search(r"## 🛠️ 2\. 跨檔案協同修復計畫.*?\n(.*?)\n\s*###", description, re.DOTALL) or \
                re.search(r"### 💡 AI 建議修復方案\s*\n(.*?)\n\s*---", description, re.DOTALL) or \
                re.search(r"## 🛠️ 建議修復方案.*?\n(.*?)\n\s*---", description, re.DOTALL)
    suggested_fix = fix_match.group(1).strip() if fix_match else description

    return file_name, line_number, suggested_fix

def ask_llm_to_heal(file_content: str, line_number: int, suggested_fix: str, last_error_log: str = None) -> str:
    """
    向大模型發送代碼與修復線索，請求修復代碼。
    """
    print(f"🤖 正在向大模型 ({LLM_MODEL}) 發送修復請求...")
    
    system_instruction = (
        "你是一位頂尖的 Python 程式碼自癒專家。請你精確修復程式碼中的 Bug。\n"
        "【重要規則】\n"
        "1. 請只修改需要修復的程式碼行，不要破壞或更動其他無關的功能與變數。\n"
        "2. 請直接返回修改後的「完整 Python 原始碼內容」。\n"
        "3. 你的回覆必須是純程式碼文本，不可以包含 ```python 等 Markdown 包裹標記，也不要夾帶任何解釋性文字。"
    )

    user_prompt = (
        f"出錯程式碼檔案內容如下：\n"
        f"```python\n{file_content}\n```\n\n"
        f"該程式碼在第 {line_number} 行發生了錯誤。\n"
        f"SRE 給予的建議修復方案：\n{suggested_fix}\n\n"
    )

    if last_error_log:
        user_prompt += (
            f"上一輪修復後跑自動測試失敗了，以下是 pytest 錯誤日誌：\n"
            f"```text\n{last_error_log}\n```\n"
            f"請根據最新的錯誤日誌重新進行代碼調整與自我修正！\n"
        )

    user_prompt += "請回傳修改後完整的程式碼內容："

    response = openai_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1
    )
    
    clean_code = response.choices[0].message.content.strip()
    # 如果大模型頑皮加了 ```python 包裹，我們用正則將其剝除
    clean_code = re.sub(r"^```python\s*", "", clean_code)
    clean_code = re.sub(r"\s*```$", "", clean_code)
    return clean_code

def run_pytest() -> tuple:
    """
    執行單元測試，返回是否成功與錯誤日誌。
    """
    print("🧪 正在執行 pytest 測試驗證...")
    # 專案主要的 API 煙霧測試
    test_cmd = ["pytest", "tests/api/test_visit_structured_endpoint.py"]
    
    # 取得當前目錄結構
    cwd = "rag_frontend/backend" if os.path.exists("rag_frontend/backend") else "."
    
    res = subprocess.run(test_cmd, capture_output=True, text=True, cwd=cwd)
    is_success = (res.returncode == 0)
    
    output_log = res.stdout + "\n" + res.stderr
    return is_success, output_log

def comment_on_issue(mr_url: str, iid: str):
    """
    自動在原本的 Issue 下方留言，附上 MR 連結通知。
    """
    print(f"💬 正在自動在 Issue #{iid} 下方進行留言通知...")
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    url = f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/issues/{iid}/notes"
    
    comment_body = (
        f"🤖 **SRE 自動化自癒大腦報告**\n\n"
        f"報告！此 Issue 的程式碼 Bug 已經完成自動修改，並且在 Runner 容器內**通過 `pytest` 測試驗證**！ ✅\n\n"
        f"👉 **[點此前往審查與合併 Merge Request]({mr_url})**"
    )
    
    payload = {"body": comment_body}
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10.0)
        if resp.status_code == 201:
            print("🎉 成功在 Issue 下方留下自動回覆！")
        else:
            print(f"⚠️ Issue 留言失敗: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ Issue 留言發生異常: {e}")

def create_merge_request(iid: str):
    """
    自癒成功後，自動開立 Merge Request (MR)。
    """
    # 取得 trigger 來源分支，若無則預設為開發測試分支
    target_branch = os.getenv("CI_COMMIT_REF_NAME", "main")
    print(f"🔀 正在向 GitLab 申請開立 Merge Request: fix/issue-{iid} ➡️ {target_branch}...")
    
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    url = f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/merge_requests"
    
    payload = {
        "source_branch": f"fix/issue-{iid}",
        "target_branch": target_branch,
        "title": f"Draft: Fix issue #{iid} automatically by SRE Remediation Agent",
        "description": (
            f"🤖 此 MR 由 SRE 自動化自癒 Agent (Remediation Agent) 建立。\n\n"
            f"**修正工單**：#{iid}\n"
            f"**品質檢驗**：已於 Runner 內自動執行 pytest 跑通綠燈。 ✅\n"
            f"**合併設定**：此 MR 已設定「合併後自動刪除來源分支（remove_source_branch）」，保持 Git 樹清爽。"
        ),
        "remove_source_branch": True
    }
    
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10.0)
        if resp.status_code == 201:
            mr_data = resp.json()
            mr_url = mr_data.get("web_url")
            print(f"🎉 MR 開立成功! 連結: {mr_url}")
            comment_on_issue(mr_url, iid)
        elif resp.status_code == 409:
            print("ℹ️ 該分支的 MR 已經存在，跳過建立。")
        else:
            print(f"⚠️ MR 開立失敗: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ MR 開立發生異常: {e}")

def save_case_to_knowledge_base(issue_iid: str, file_name: str, suggested_fix: str):
    """將修復成功案例存檔至 knowledge_base/cases/"""
    case_dir = os.path.join(ROOT_DIR, "knowledge_base", "cases")
    os.makedirs(case_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    case_file = os.path.join(case_dir, f"case_{date_str}_issue_{issue_iid}.json")
    try:
        with open(case_file, "w", encoding="utf-8") as f:
            json.dump({
                "issue_iid": issue_iid,
                "file_name": file_name,
                "suggested_fix": suggested_fix,
                "healed_at": datetime.now().isoformat(),
                "status": "VERIFIED_GREEN"
            }, f, ensure_ascii=False, indent=2)
        print(f"📚 成功將修復經驗沉澱至知識庫: {case_file}")
    except Exception as e:
        print(f"⚠️ 儲存知識庫案例失敗: {e}")

def push_healed_code(iid: str):
    """
    在 pytest 綠燈後，自動提交並 push 修復分支，並觸發 MR 建立與 Issue 留言。
    """
    print("📤 測試完全通過！正在將自癒代碼 Push 至 GitLab...")
    try:
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", f"fix(sre): auto heal issue #{iid} with pytest green light"], check=True)
        subprocess.run(["git", "push", "origin", f"fix/issue-{iid}"], check=True)
        print("🎉 自癒代碼成功 Push 至遠端分支！")
        # 建立 MR 暨 Issue 留言
        create_merge_request(iid)
    except Exception as e:
        print(f"❌ Git 操作失敗: {e}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SRE Remediation Agent: Code Repair & Auto Merge Request")
    parser.add_argument("--issue-iid", type=str, default=os.getenv("ISSUE_IID"), help="GitLab Issue IID")
    args = parser.parse_args()
    
    iid = args.issue_iid
    if not iid:
        print("⚠️ 未指定 --issue-iid 或未檢測到 ISSUE_IID 環境變數，跳過自癒流程。")
        sys.exit(0)

    try:
        # 1. 取得 Issue 內容
        description = fetch_issue_description(iid)
        file_name, line_number, suggested_fix = parse_issue_description(description)
        
        if not file_name:
            print("❌ 無法從 Issue 內文中解析出出錯檔案位置！")
            sys.exit(1)
            
        print(f"📍 診斷出錯檔案: {file_name} (行號: {line_number})")
        
        # 2. 本地定位實體檔案路徑
        target_path = find_target_file(file_name)
        if not target_path:
            print(f"❌ 本地找不到名稱為 '{file_name}' 的檔案！")
            sys.exit(1)
            
        print(f"📂 尋找到本地檔案實體路徑: {target_path}")

        # 讀取原本的壞程式碼
        with open(target_path, "r", encoding="utf-8") as f:
            original_content = f.read()

        # 3. 進入「修復 ➡️ 測試 ➡️ 自我修正」循環（最多 3 次）
        healed_content = original_content
        error_log = None
        for attempt in range(1, 4):
            print(f"\n🔄 正在嘗試第 {attempt} 次自癒修改與驗證...")
            
            # 呼叫 LLM 取得修復程式碼
            healed_content = ask_llm_to_heal(original_content, line_number, suggested_fix, last_error_log=error_log)
            
            # 覆寫檔案
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(healed_content)
                
            # 執行測試驗證
            is_ok, error_log = run_pytest()
            if is_ok:
                print(f"✅ 第 {attempt} 次嘗試修復：Pytest 綠燈通過！")
                save_case_to_knowledge_base(iid, file_name, suggested_fix)
                push_healed_code(iid)
                sys.exit(0)
            else:
                print(f"❌ 第 {attempt} 次嘗試修復失敗，測試未通過！")
                # 限制 log 長度，避免超過 LLM 限制
                error_log = error_log[-1500:] 

        print("❌ 自癒 Agent 經歷 3 次重試修正，依然無法跑通 pytest，修復宣告失敗。")
        sys.exit(1)

    except Exception as e:
        print(f"❌ 自癒過程發生例外中斷: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
