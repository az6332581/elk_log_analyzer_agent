from __future__ import annotations

import json
from .code_tools import fetch_code_snippet, read_file_range
from .search_tools import search_symbol_definition

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "fetch_code_snippet",
            "description": "【SRE 技能】依據報錯檔案與行號，只讀取出錯行前後 20 行真實原始碼（附帶行號與 👉 標記），避免 Token 爆炸。",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "微服務專案名稱 (如 socialwork2-rag-backend)"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "專案內出錯檔案相對路徑 (如 app/services/llm_client.py)"
                    },
                    "line_number": {
                        "type": "integer",
                        "description": "日誌指示的出錯行號"
                    },
                    "window": {
                        "type": "integer",
                        "description": "前後延伸查看行數，預設 20"
                    }
                },
                "required": ["service_name", "file_path", "line_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_range",
            "description": "【SRE 技能】閱讀指定檔案特定行號範圍（例如查看 config.py 或 schemas.py 定義），一次上限 100 行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "微服務專案名稱"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "檔案相對路徑"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "開始行號 (1-indexed)"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "結束行號"
                    }
                },
                "required": ["service_name", "file_path", "start_line", "end_line"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbol_definition",
            "description": "【SRE 技能】在專案中搜尋某個函式、Class 或變數的定義位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "微服務專案名稱"
                    },
                    "symbol_name": {
                        "type": "string",
                        "description": "要搜尋的函式或類別名稱"
                    }
                },
                "required": ["service_name", "symbol_name"]
            }
        }
    }
]

def execute_skill(name: str, arguments: dict | str) -> str:
    """執行大模型請求發動的技能"""
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except Exception:
            args = {}
    else:
        args = arguments or {}

    if name == "fetch_code_snippet":
        return fetch_code_snippet(
            service_name=args.get("service_name", ""),
            file_path=args.get("file_path", ""),
            line_number=int(args.get("line_number", 1)),
            window=int(args.get("window", 20))
        )
    elif name == "read_file_range":
        return read_file_range(
            service_name=args.get("service_name", ""),
            file_path=args.get("file_path", ""),
            start_line=int(args.get("start_line", 1)),
            end_line=int(args.get("end_line", 50))
        )
    elif name == "search_symbol_definition":
        return search_symbol_definition(
            service_name=args.get("service_name", ""),
            symbol_name=args.get("symbol_name", "")
        )
    else:
        return f"⚠️ 未知技能: {name}"
