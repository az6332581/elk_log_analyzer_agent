from __future__ import annotations
import os, json, subprocess
from urllib.parse import urlparse
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))
load_dotenv()

GITLAB_URL = os.getenv("GITLAB_URL", "http://192.168.20.112:8929")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
REPOS_DIR = os.path.join(ROOT_DIR, "repos")
PROJECTS_CONFIG_PATH = os.path.join(ROOT_DIR, "projects.json")
BACKUP_CONFIG_PATH = os.path.join(ROOT_DIR, "reports", ".projects_backup.json")

def load_projects_config() -> dict:
    """
    從資料庫加載微服務註冊設定；若 DB 尚未初始化則自癒從 projects.json 匯入。
    若 DB 失敗則平滑降級至 projects.json 與備份檔。
    """
    data = {}
    try:
        from core.database import get_db_session, Project, init_db
        session = get_db_session()
        projects = session.query(Project).all()
        if projects:
            for p in projects:
                data[p.service_name] = p.to_dict()
        else:
            # 資料庫尚空，自動嘗試初始化匯入
            init_db()
            projects = session.query(Project).all()
            for p in projects:
                data[p.service_name] = p.to_dict()
        session.close()
    except Exception as e:
        print(f"[Repo Manager] DB load projects failed (fallback to file): {e}")

    # 若 DB 未取得任何資料，降級從 projects.json 讀取
    if not data and os.path.exists(PROJECTS_CONFIG_PATH):
        try:
            with open(PROJECTS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[Repo Manager] Load projects.json failed: {e}")
            data = {}
            
    # 🛡️ 防呆自癒機制：若 projects.json 遺失或損壞，但備份檔存在，自動從備份搶救還原
    if not data and os.path.exists(BACKUP_CONFIG_PATH):
        try:
            with open(BACKUP_CONFIG_PATH, "r", encoding="utf-8") as bf:
                backup_data = json.load(bf)
            if backup_data and isinstance(backup_data, dict):
                print(f"🛡️ [Repo Manager] 檢測到 projects.json 異常，已自動從備份檔自癒恢復 {len(backup_data)} 個微服務設定！")
                save_projects_config(backup_data)
                return backup_data
        except Exception as be:
            print(f"[Repo Manager] 備份救援還原失敗: {be}")
            
    # 若當前設定存在但尚未建立備份，順手留存初始備份
    if data and not os.path.exists(BACKUP_CONFIG_PATH):
        try:
            os.makedirs(os.path.dirname(BACKUP_CONFIG_PATH), exist_ok=True)
            with open(BACKUP_CONFIG_PATH, "w", encoding="utf-8") as bf:
                json.dump(data, bf, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return data

def save_projects_config(configs: dict) -> bool:
    """
    雙軌同步：儲存至資料庫 Project 表，同時鏡像寫入 projects.json 與 reports/.projects_backup.json
    """
    db_success = False
    try:
        from core.database import get_db_session, Project
        session = get_db_session()
        # 清除現有或比對 upsert
        current_names = set(configs.keys())
        # 刪除已不存在的
        session.query(Project).filter(~Project.service_name.in_(current_names)).delete(synchronize_session=False)
        for sname, conf in configs.items():
            proj = session.query(Project).filter_by(service_name=sname).first()
            if not proj:
                proj = Project(service_name=sname)
                session.add(proj)
            proj.project_path = conf.get("project_path", "")
            proj.project_id = conf.get("project_id")
            proj.branch = conf.get("branch", "main")
            proj.description = conf.get("description", "")
            proj.log_field = conf.get("log_field", "")
            proj.log_value = conf.get("log_value", "")
            proj.notification_emails = conf.get("notification_emails", "")
            proj.email_alert_enabled = conf.get("email_alert_enabled", True)
        session.commit()
        session.close()
        db_success = True
    except Exception as dbe:
        print(f"[Repo Manager] DB save projects failed: {dbe}")

    try:
        with open(PROJECTS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
            
        # 🛡️ 雙重保險：每次變更自動鏡像備份至 reports/.projects_backup.json
        if configs:
            try:
                os.makedirs(os.path.dirname(BACKUP_CONFIG_PATH), exist_ok=True)
                with open(BACKUP_CONFIG_PATH, "w", encoding="utf-8") as bf:
                    json.dump(configs, bf, ensure_ascii=False, indent=2)
            except Exception as be:
                print(f"[Repo Manager] Warning: mirror backup failed: {be}")
                
        return True
    except Exception as e:
        print(f"[Repo Manager] Save projects.json failed: {e}")
        return db_success

def normalize_project_path(path: str) -> str:
    """自動將任何 GitLab URL (HTTP/HTTPS/SSH/相對路徑) 正規化為 group/project 格式"""
    clean = path.strip()
    if clean.startswith("http://") or clean.startswith("https://"):
        parsed = urlparse(clean)
        clean = parsed.path
    elif ":" in clean and "@" in clean:
        clean = clean.split(":")[-1]
    clean = clean.strip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    return clean

import urllib.parse
import httpx

def fetch_gitlab_branches(project_path_or_id: str | int) -> list[str]:
    """
    透過 GitLab API 自動撈取指定專案的所有遠端分支名稱。
    若 GitLab Token 失效或無權限，回傳空列表，不拋出中斷異常。
    """
    if not project_path_or_id or not GITLAB_URL or not GITLAB_TOKEN:
        return []
    try:
        if isinstance(project_path_or_id, int) or str(project_path_or_id).isdigit():
            target_id = str(project_path_or_id)
        else:
            clean_path = normalize_project_path(str(project_path_or_id))
            target_id = urllib.parse.quote(clean_path, safe="")
            
        resp = httpx.get(
            f"{GITLAB_URL.rstrip('/')}/api/v4/projects/{target_id}/repository/branches",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            timeout=5.0
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return [b.get("name") for b in data if isinstance(b, dict) and b.get("name")]
        else:
            print(f"[Repo Manager] Fetch branches HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"[Repo Manager] Fetch branches exception: {e}")
    return []

def resolve_gitlab_project_id(project_path: str) -> int | None:
    """透過 GitLab API 依據路徑自動取得真實 Project ID"""
    if not project_path or not GITLAB_URL or not GITLAB_TOKEN:
        return None
    try:
        clean_path = normalize_project_path(project_path)
        encoded = urllib.parse.quote(clean_path, safe="")
        resp = httpx.get(
            f"{GITLAB_URL.rstrip('/')}/api/v4/projects/{encoded}",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            timeout=5.0
        )
        if resp.status_code == 200:
            return resp.json().get("id")
    except Exception as e:
        print(f"[Repo Manager] Auto-resolve project ID failed: {e}")
    return None

def add_or_update_project(service_name: str, config: dict) -> bool:
    configs = load_projects_config()
    if "project_path" in config and config["project_path"]:
        config["project_path"] = normalize_project_path(config["project_path"])
        # 自動校正或補齊真實的 GitLab Project ID
        real_id = resolve_gitlab_project_id(config["project_path"])
        if real_id:
            config["project_id"] = real_id
    # 完全保留使用者指定的分支 (config['branch'])，不自動覆寫
    configs[service_name] = config
    return save_projects_config(configs)

import stat, shutil

def _force_remove_readonly(func, path, excinfo):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass

def delete_project(service_name: str, purge_files: bool = True) -> bool:
    configs = load_projects_config()
    existed = False
    if service_name in configs:
        del configs[service_name]
        save_projects_config(configs)
        existed = True

    try:
        from core.database import get_db_session, Project
        session = get_db_session()
        p = session.query(Project).filter_by(service_name=service_name).first()
        if p:
            session.delete(p)
            session.commit()
            existed = True
        session.close()
    except Exception as dbe:
        print(f"[Repo Manager] DB delete project failed: {dbe}")

    if purge_files:
        # 1. 清理 repos/<service_name> 程式碼鏡像
        repo_path = os.path.join(REPOS_DIR, service_name)
        if os.path.exists(repo_path):
            try:
                shutil.rmtree(repo_path, onerror=_force_remove_readonly)
                print(f"[Repo Manager] Purged repo code: {repo_path}")
                existed = True
            except Exception as e:
                print(f"[Repo Manager] Failed to remove {repo_path}: {e}")

        # 2. 清理 reports/<service_name> 報告資料夾
        reports_dir = os.path.join(ROOT_DIR, "reports", service_name)
        if os.path.exists(reports_dir):
            try:
                shutil.rmtree(reports_dir, onerror=_force_remove_readonly)
                print(f"[Repo Manager] Purged reports folder: {reports_dir}")
                existed = True
            except Exception as e:
                print(f"[Repo Manager] Failed to remove {reports_dir}: {e}")

        # 3. 重新整理 reports/index.md 索引
        try:
            from core.report_generator import update_reports_index_dashboard
            update_reports_index_dashboard(os.path.join(ROOT_DIR, "reports"))
        except Exception:
            pass

    return existed

def get_project_config(service_name: str) -> dict | None:
    configs = load_projects_config()
    return configs.get(service_name)

def get_authenticated_clone_url(project_path: str) -> str:
    parsed = urlparse(GITLAB_URL)
    token = GITLAB_TOKEN or ""
    if token:
        netloc = f"oauth2:{token}@" + str(parsed.hostname)
        if parsed.port:
            netloc += f":{parsed.port}"
    else:
        netloc = parsed.netloc
    clean_path = normalize_project_path(project_path)
    return f"{parsed.scheme}://{netloc}/{clean_path}.git"

def sync_repo(service_name: str) -> str | None:
    config = get_project_config(service_name)
    if not config:
        print(f"[Repo Sync] '{service_name}' not in projects.json, skip code sync.")
        return None

    os.makedirs(REPOS_DIR, exist_ok=True)
    target_dir = os.path.join(REPOS_DIR, service_name)
    branch = config.get("branch", "main")
    project_path = config.get("project_path")
    if not project_path:
        return None

    clone_url = get_authenticated_clone_url(project_path)

    try:
        if not os.path.exists(os.path.join(target_dir, ".git")):
            print(f"[Repo Sync] First shallow clone '{service_name}' (branch: {branch})...")
            cmd = ["git", "clone", "--depth", "1", "-b", branch, clone_url, target_dir]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[Repo Sync] Clone failed: {res.stderr.strip()}")
                return None
            print(f"[Repo Sync] Clone completed: {target_dir}")
        else:
            print(f"[Repo Sync] Updating '{service_name}' to latest {branch}...")
            subprocess.run(["git", "-C", target_dir, "fetch", "origin", branch], capture_output=True, check=True)
            subprocess.run(["git", "-C", target_dir, "reset", "--hard", f"origin/{branch}"], capture_output=True, check=True)
            print(f"[Repo Sync] Update completed: {target_dir}")
        return target_dir
    except Exception as e:
        print(f"[Repo Sync] Sync exception: {e}")
        return None