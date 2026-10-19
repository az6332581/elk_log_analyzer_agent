from __future__ import annotations

import os
import re

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_DIR = os.path.join(ROOT_DIR, "repos")

def search_symbol_definition(service_name: str, symbol_name: str) -> str:
    """在專案代碼中搜尋特定函式、類別或變數的定義位置"""
    repo_dir = os.path.join(REPOS_DIR, service_name)
    if not os.path.isdir(repo_dir):
        repo_dir = ROOT_DIR

    patterns = [
        re.compile(rf"^\s*(def|class)\s+{re.escape(symbol_name)}\b"),
        re.compile(rf"^\s*{re.escape(symbol_name)}\s*="),
    ]

    matches = []
    for root, dirs, files in os.walk(repo_dir):
        if any(p in root for p in [".git", ".venv", "__pycache__", "node_modules"]):
            continue
        for file in files:
            if not file.endswith((".py", ".js", ".ts", ".json")):
                continue
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, repo_dir)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, start=1):
                        for p in patterns:
                            if p.search(line):
                                matches.append(f"📍 {rel_path}:{line_no} | {line.strip()}")
                                if len(matches) >= 10:
                                    break
            except Exception:
                pass
            if len(matches) >= 10:
                break
        if len(matches) >= 10:
            break

    if not matches:
        return f"ℹ️ 在專案 {service_name} 中未找到符號 '{symbol_name}' 的明顯定義。"

    return "【搜尋到之符號定義位置】\n" + "\n".join(matches)
