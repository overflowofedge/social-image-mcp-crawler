"""Local platform sessions shared by the desktop app and CLI bridges."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = {"douyin": "抖音", "weibo": "微博"}
DOMAINS = {"douyin": ("douyin.com",), "weibo": ("weibo.com", "weibo.cn")}
AUTH_NAMES = {"douyin": {"sessionid", "sessionid_ss"}, "weibo": {"SUB"}}


def session_path(platform: str, root: Path = ROOT, account: str | None = None) -> Path:
    if platform not in PLATFORMS:
        raise ValueError("目前应用内扫码登录支持抖音和微博。")
    if platform == "weibo":
        configured = os.getenv("WEIBO_BROWSER_STORAGE_STATE")
        path = Path(configured).expanduser() if configured else root / ".cache/weibo-session.json"
    else:
        configured = (os.getenv("DOUYIN_BROWSER_STORAGE_STATE") or os.getenv("DY_CLI_STORAGE_STATE")
                      or os.getenv("DOUYIN_COOKIE_FILE"))
        if configured:
            path = Path(configured).expanduser()
        else:
            if not account:
                account = os.getenv("DY_CLI_ACCOUNT")
                if not account:
                    try:
                        config = json.loads((Path.home() / ".dy/config.json").read_text(encoding="utf-8"))
                        account = config.get("default", {}).get("account")
                    except (OSError, ValueError, AttributeError):
                        pass
            # Account names must remain filenames inside dy-cli's cookie directory.
            account = str(account or "default")
            if any(part in account for part in ("/", "\\", "..")):
                raise ValueError("抖音账号配置名称无效。")
            path = Path.home() / ".dy/cookies" / f"{account}.json"
    return path if path.is_absolute() else root / path


def platform_cookies(platform: str, state: dict, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    rows = state.get("cookies", [])
    if not isinstance(rows, list):
        return []
    result = []
    for cookie in rows:
        if not isinstance(cookie, dict) or not cookie.get("name") or not cookie.get("value"):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if not any(domain == allowed or domain.endswith("." + allowed) for allowed in DOMAINS[platform]):
            continue
        try:
            expiry = float(cookie.get("expires", -1))
        except (ValueError, TypeError):
            continue
        if expiry > 0 and expiry <= now:
            continue
        result.append(cookie)
    return result


def has_auth(platform: str, state: dict) -> bool:
    return any(cookie["name"] in AUTH_NAMES[platform] for cookie in platform_cookies(platform, state))


def read_session(platform: str, root: Path = ROOT, account: str | None = None) -> dict:
    try:
        state = json.loads(session_path(platform, root, account).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def cookie_header(platform: str, state: dict, url: str) -> str:
    host = urlparse(url).hostname or ""
    cookies = platform_cookies(platform, state)
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies
                     if host == cookie["domain"].lstrip(".")
                     or host.endswith("." + cookie["domain"].lstrip(".")))


def save_session(platform: str, state: dict, root: Path = ROOT) -> None:
    if not has_auth(platform, state):
        raise ValueError("尚未获取有效登录信息，请完成手机扫码确认。")
    # Keep only this platform's data. Cookies never enter API responses or logs.
    state = {"cookies": platform_cookies(platform, state), "origins": [
        origin for origin in state.get("origins", []) if isinstance(origin, dict)
        and any((urlparse(str(origin.get("origin", ""))).hostname or "") == domain
                or (urlparse(str(origin.get("origin", ""))).hostname or "").endswith("." + domain)
                for domain in DOMAINS[platform])
    ]}
    target = session_path(platform, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
