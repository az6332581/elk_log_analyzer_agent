from __future__ import annotations

import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_DIR = os.path.join(ROOT_DIR, "repos")

def _locate_file_in_repo(repo_dir: str, file_path: str) -> str | None:
    clean_path = file_path.lstrip("/\\")
    direct_path = os.path.join(repo_dir, clean_path)
    if os.path.isfile(direct_path):
        return direct_path

    base_name = os.path.basename(clean_path)
    for root, dirs, files in os.walk(repo_dir):
        if any(p in root for p in [".git", ".venv", "__pycache__", "node_modules"]):
            continue
        if base_name in files:
            return os.path.join(root, base_name)
    return None

def fetch_code_snippet(service_name: str, file_path: str, line_number: int, window: int = 20, stack_trace: str = None) -> str:
    """截取出錯檔案指定行號前後上下文 (預設前後 20 行，支援第三方依賴自動回溯)"""
    import re
    repo_dir = os.path.join(REPOS_DIR, service_name)
    if not os.path.isdir(repo_dir):
        repo_dir = ROOT_DIR

    target_file = _locate_file_in_repo(repo_dir, file_path)
    fallback_note = ""
    
    # 若檔案找不到（常發生在第三方庫如 _models.py, socket.py），嘗試從 stack_trace 倒序回溯專案內的檔案
    if not target_file and stack_trace:
        matches = re.findall(r'File "([^"]+)", line (\d+)', stack_trace)
        for s_file, s_line in reversed(matches):
            cand = _locate_file_in_repo(repo_dir, s_file)
            if cand:
                target_file = cand
                file_path = s_file
                line_number = int(s_line)
                fallback_note = f"// 💡 原始出錯點位於第三方依賴庫，已自動為您回溯定位至專案內部呼叫點: {s_file}:{s_line}\n"
                break

    if not target_file:
        return f"⚠️ 找不到檔案: {file_path} (專案: {service_name}，且堆疊中無可匹配的專案內部檔案)"

    try:
        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        return f"⚠️ 讀取檔案失敗: {file_path} ({e})"

    total_lines = len(lines)
    if total_lines == 0:
        return f"⚠️ 檔案為空: {file_path}"

    start_idx = max(0, line_number - window - 1)
    end_idx = min(total_lines, line_number + window)
    snippet_lines = []
    if fallback_note:
        snippet_lines.append(fallback_note.strip())
    snippet_lines.append(f"// 📄 檔案路徑: {file_path} (顯示第 {start_idx + 1} ~ {end_idx} 行，共 {total_lines} 行)")
    for i in range(start_idx, end_idx):
        curr_line = i + 1
        marker = "👉 " if curr_line == line_number else "   "
        line_content = lines[i].rstrip("\r\n")
        snippet_lines.append(f"{marker}{curr_line:4d} | {line_content}")

    return "\n".join(snippet_lines)

def read_file_range(service_name: str, file_path: str, start_line: int, end_line: int) -> str:
    """閱讀指定檔案之行號區間 (最多 100 行)"""
    repo_dir = os.path.join(REPOS_DIR, service_name)
    if not os.path.isdir(repo_dir):
        repo_dir = ROOT_DIR

    target_file = _locate_file_in_repo(repo_dir, file_path)
    if not target_file:
        return f"⚠️ 找不到檔案: {file_path} (專案: {service_name})"

    try:
        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        return f"⚠️ 讀取檔案失敗: {file_path} ({e})"

    total_lines = len(lines)
    s = max(1, start_line)
    e = min(total_lines, end_line)
    if e - s > 100:
        e = s + 100

    snippet_lines = [f"// 📄 檔案區間: {file_path} (第 {s} ~ {e} 行)"]
    for i in range(s - 1, e):
        curr_line = i + 1
        line_content = lines[i].rstrip("\r\n")
        snippet_lines.append(f"   {curr_line:4d} | {line_content}")

    return "\n".join(snippet_lines)
