"""Local platform sessions shared by the desktop app and CLI bridges."""
from __future__ import annotations

import json
import http.cookiejar
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = {"douyin": "抖音", "weibo": "微博", "xhs": "小红书", "bilibili": "B站",
             "x": "X", "instagram": "Instagram"}
QR_PLATFORMS = {"douyin", "weibo", "xhs", "bilibili"}
DOMAINS = {"douyin": ("douyin.com",), "weibo": ("weibo.com", "weibo.cn"),
           "xhs": ("xiaohongshu.com",), "bilibili": ("bilibili.com",),
           "x": ("x.com", "twitter.com"), "instagram": ("instagram.com",)}
AUTH_NAMES = {"douyin": {"sessionid", "sessionid_ss"}, "weibo": {"SUB"},
              "xhs": {"web_session"}, "bilibili": {"SESSDATA"},
              "x": {"auth_token"}, "instagram": {"sessionid"}}


def session_path(platform: str, root: Path = ROOT, account: str | None = None) -> Path:
    if platform not in PLATFORMS:
        raise ValueError("请选择支持的平台登录；普通网页无需平台账号。")
    if platform != "douyin":
        configured = os.getenv(f"{platform.upper()}_BROWSER_STORAGE_STATE")
        path = Path(configured).expanduser() if configured else root / f".cache/{platform}-session.json"
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
    path = session_path(platform, root, account)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        # Keep the sessions exported by the previous standalone setup usable.
        if platform in {"x", "instagram"} and not path.exists():
            configured = os.getenv(f"{platform.upper()}_GALLERY_DL_COOKIES_FILE")
            fallback = Path(configured).expanduser() if configured else root / f".cache/gallery-dl-{platform}-cookies.txt"
            if not fallback.is_absolute():
                fallback = root / fallback
            try:
                jar = http.cookiejar.MozillaCookieJar(str(fallback))
                # Netscape files use 0 for session cookies. Authentication and
                # real expiration are filtered by platform_cookies below.
                jar.load(ignore_discard=True, ignore_expires=True)
                return {"cookies": [{"name": cookie.name, "value": cookie.value,
                         "domain": cookie.domain, "path": cookie.path, "secure": cookie.secure,
                         "expires": cookie.expires or -1} for cookie in jar], "origins": []}
            except (OSError, ValueError, http.cookiejar.LoadError):
                pass
        return {}


def cookie_header(platform: str, state: dict, url: str) -> str:
    host = urlparse(url).hostname or ""
    cookies = platform_cookies(platform, state)
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies
                     if host == cookie["domain"].lstrip(".")
                     or host.endswith("." + cookie["domain"].lstrip(".")))


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def gallery_cookie_file(platform: str, state: dict, root: Path = ROOT) -> Path:
    """Export only the requested platform for gallery-dl without browser decryption."""
    if platform not in {"x", "instagram"} or not has_auth(platform, state):
        raise ValueError("此平台缺少可用于采集的登录会话。")
    target = root / f".cache/gallery-dl-{platform}-cookies.txt"
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in platform_cookies(platform, state):
        domain = cookie["domain"]
        expiry = float(cookie.get("expires", -1))
        fields = (domain, "TRUE" if domain.startswith(".") else "FALSE", cookie.get("path") or "/",
                  "TRUE" if cookie.get("secure") else "FALSE", str(int(expiry)) if expiry > 0 else "",
                  cookie["name"], cookie["value"])
        if any("\n" in str(value) or "\r" in str(value) or "\t" in str(value) for value in fields):
            raise ValueError("登录文件内容格式无效，请重新登录。")
        lines.append("\t".join(str(value) for value in fields))
    text = "\n".join(lines) + "\n"
    try:
        if target.read_text(encoding="utf-8") == text:
            return target
    except (OSError, UnicodeDecodeError):
        pass
    _atomic_write(target, text)
    return target


def save_session(platform: str, state: dict, root: Path = ROOT) -> None:
    if not has_auth(platform, state):
        raise ValueError("尚未获取有效登录信息，请完成官方登录及确认。")
    # Keep only this platform's data. Cookies never enter API responses or logs.
    state = {"cookies": platform_cookies(platform, state), "origins": [
        origin for origin in state.get("origins", []) if isinstance(origin, dict)
        and any((urlparse(str(origin.get("origin", ""))).hostname or "") == domain
                or (urlparse(str(origin.get("origin", ""))).hostname or "").endswith("." + domain)
                for domain in DOMAINS[platform])
    ]}
    _atomic_write(session_path(platform, root), json.dumps(state, ensure_ascii=False))
