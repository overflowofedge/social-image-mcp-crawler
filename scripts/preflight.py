from __future__ import annotations

"""Check and repair the desktop crawler before its local UI starts."""

import argparse
import json
import os
import subprocess
import sys
import time
import shutil
from pathlib import Path
from typing import Any


AUTH_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt", "uid_tt_ss"}


def inspect_cookie_file(path: Path, now: float | None = None) -> dict[str, Any]:
    """Inspect dy-cli storage state without exposing cookie values."""
    now = time.time() if now is None else now
    if not path.is_file():
        return {"state": "missing", "path": str(path), "cookie_count": 0, "auth_cookie_count": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"state": "invalid", "path": str(path), "cookie_count": 0, "auth_cookie_count": 0, "error": str(exc)}
    rows = payload.get("cookies") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {"state": "invalid", "path": str(path), "cookie_count": 0, "auth_cookie_count": 0, "error": "cookies must be a list"}
    douyin_rows = [row for row in rows if isinstance(row, dict) and "douyin" in str(row.get("domain", "")).lower()]
    auth_rows = [row for row in douyin_rows if str(row.get("name", "")).lower() in AUTH_COOKIE_NAMES and str(row.get("value", ""))]
    expired: list[str] = []
    active = []
    for row in auth_rows:
        try:
            expires = float(row.get("expires", -1))
        except (TypeError, ValueError):
            expires = -1
        if expires > 0 and expires <= now:
            expired.append(str(row.get("name", "")))
        else:
            active.append(row)
    state = "missing_auth" if not auth_rows else ("expired" if not active else "valid")
    return {
        "state": state,
        "path": str(path),
        "cookie_count": len(douyin_rows),
        "auth_cookie_count": len(auth_rows),
        "expired_auth_cookies": sorted(expired),
        "updated_at": path.stat().st_mtime,
    }


def cookie_file_path() -> Path:
    configured = os.getenv("DOUYIN_COOKIE_FILE")
    if configured:
        return Path(configured).expanduser()
    config_file = Path.home() / ".dy" / "config.json"
    account = os.getenv("DY_CLI_ACCOUNT") or "default"
    try:
        payload = json.loads(config_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            account = str(payload.get("default", {}).get("account") or account)
            configured = payload.get("api", {}).get("cookie_file")
            if configured:
                return Path(str(configured)).expanduser()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    return Path.home() / ".dy" / "cookies" / f"{account}.json"


def _run(command: list[str], timeout: float, env: dict[str, str] | None = None, cwd: Path | None = None) -> tuple[int, str, str]:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd)
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout.strip(), stderr.strip()
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        return 124, "", f"timeout after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)


def _check_imports(python: Path) -> dict[str, Any]:
    rc, out, err = _run([str(python), "-c", "import httpx, imageio_ffmpeg, mcp, PIL, playwright; print('ok')"], 20)
    return {"ok": rc == 0 and out.endswith("ok"), "returncode": rc, "detail": err or out}


def _browser_channel() -> str | None:
    configured = os.getenv("BROWSER_CHANNEL") or os.getenv("DOUYIN_BROWSER_CHANNEL")
    if configured:
        return configured
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    return "msedge" if any(candidate and Path(candidate).is_file() for candidate in candidates) else None


def _check_browser(python: Path) -> dict[str, Any]:
    channel = _browser_channel()
    launch = f"channel={channel!r},headless=True" if channel else "headless=True"
    code = f"from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch({launch}); b.close(); p.stop(); print('ok')"
    rc, out, err = _run([str(python), "-c", code], 35)
    return {"ok": rc == 0 and out.endswith("ok"), "returncode": rc, "channel": channel, "detail": err or out}


def _check_bridge(project_root: Path) -> dict[str, Any]:
    bridge = project_root / "scripts" / "douyin_cli_bridge.py"
    source_root = project_root / "third_party" / "dy-cli"
    ok = bridge.is_file() and (source_root / "src/dy_cli/main.py").is_file()
    return {"ok": ok, "configured": source_root.is_dir(), "bridge": str(bridge), "source_root": str(source_root), "detail": "ready" if ok else "抖音来源未安装，抖音功能不可用"}


def _check_douyin_imports(project_root: Path, python: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "third_party/dy-cli/src")
    code = "from dy_cli.engines.api_client import DouyinAPIClient; from dy_cli.utils.signature import close_sign_page; import dy_cli.main; print('ok')"
    rc, out, err = _run([str(python), "-c", code], 35, env, project_root)
    return {"ok": rc == 0 and out.endswith("ok"), "detail": (err or out)[-1200:]}


def _check_gallery(python: Path) -> dict[str, Any]:
    rc, out, err = _run([str(python), "-m", "gallery_dl", "--version"], 20)
    binary = python.parent / ("gallery-dl.exe" if os.name == "nt" else "gallery-dl")
    return {"ok": rc == 0 and binary.is_file(), "binary": str(binary), "detail": err or out}


def _check_xhs_bridge(project_root: Path, python: Path) -> dict[str, Any]:
    source_root = project_root / "third_party/XHS-Downloader"
    if not (source_root / "source/__init__.py").is_file():
        return {"ok": False, "configured": False, "detail": "XHS-Downloader source is not installed"}
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)
    bridge = project_root / "scripts/xhs_downloader_bridge.py"
    code = f"import runpy; m=runpy.run_path({str(bridge)!r}); m['_prepare_vendor_imports'](); from source import XHS, Settings; print('ok')"
    rc, out, err = _run([str(python), "-c", code], 35, env, source_root)
    return {"ok": rc == 0 and out.endswith("ok"), "configured": True, "detail": (err or out)[-1200:]}


def _check_douyin_health(project_root: Path, python: Path) -> dict[str, Any]:
    bridge = project_root / "scripts" / "douyin_cli_bridge.py"
    if not bridge.is_file():
        return {"ok": False, "skipped": True, "detail": "bridge is missing"}
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    timeout = max(10, int(os.getenv("PREFLIGHT_HEALTH_TIMEOUT_SECONDS", "35")))
    rc, stdout, stderr = _run([str(python), str(bridge), "--health-check"], timeout, env)
    response: Any = None
    try:
        response = json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        pass
    return {"ok": rc == 0 and isinstance(response, dict) and response.get("ok") is True, "returncode": rc, "detail": (stderr or stdout)[-1200:], "response": response}


def _check_weibo_bridge(project_root: Path, python: Path) -> dict[str, Any]:
    source_root = project_root / "third_party" / "MediaCrawler"
    if not (source_root / "main.py").is_file():
        return {"ok": False, "configured": False, "detail": "MediaCrawler source is not installed"}
    code = "import main; print('ok')"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    rc, out, err = _run([str(python), "-c", code], 35, env, source_root)
    ready = rc == 0 and out.endswith("ok")
    return {"ok": ready, "configured": True, "returncode": rc,
            "detail": "bridge imports ready; browser login not verified" if ready else (err or out)[-1200:]}


def _check_bilibili_bridge(project_root: Path, python: Path) -> dict[str, Any]:
    """Check the dedicated Bilibili CLI without making startup depend on it."""
    bridge = project_root / "scripts" / "bilibili_cli_bridge.py"
    if not bridge.is_file():
        return {"ok": False, "configured": False, "detail": "Bilibili CLI bridge is missing"}
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    timeout = max(8, int(os.getenv("PREFLIGHT_HEALTH_TIMEOUT_SECONDS", "35")))
    live = os.getenv("PREFLIGHT_LIVE_CHECKS", "false").lower() in {"1", "true", "yes"}
    if not live:
        rc, out, err = _run([str(python), str(bridge), "--help"], timeout, env)
        return {"ok": rc == 0, "configured": True, "deferred": True, "detail": err or "CLI ready; live access checked during collection"}
    rc, stdout, stderr = _run([str(python), str(bridge), "--health-check"], timeout, env)
    response: Any = None
    try:
        response = json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        pass
    return {
        "ok": rc == 0 and isinstance(response, dict) and response.get("ok") is True,
        "configured": True,
        "returncode": rc,
        "detail": (stderr or stdout)[-1200:],
        "response": response,
    }


def _check_weibo_api() -> dict[str, Any]:
    try:
        import httpx
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36", "Referer": "https://m.weibo.cn/"}
        if cookie := os.getenv("WEIBO_COOKIE"):
            headers["Cookie"] = cookie
        response = httpx.get("https://m.weibo.cn/api/config", headers=headers, timeout=8, follow_redirects=True)
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            return {"ok": False, "status_code": response.status_code, "detail": "Weibo API is blocked or requires login"}
        payload = response.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        authenticated = bool(data.get("login"))
        if not authenticated:
            return {"ok": True, "authenticated": False, "creator_lookup_ok": False,
                    "detail": "API reachable; no native Weibo login session"}
        lookup = httpx.get(
            "https://m.weibo.cn/api/container/getIndex",
            params={"containerid": "100103type=3&q=微博", "page_type": "searchall", "page": 1},
            headers=headers, timeout=8, follow_redirects=True,
        )
        lookup_payload = lookup.json() if lookup.status_code == 200 and "json" in lookup.headers.get("content-type", "") else None
        lookup_ok = isinstance(lookup_payload, dict) and lookup_payload.get("ok") in (1, "1", True)
        return {"ok": True, "authenticated": True, "creator_lookup_ok": lookup_ok,
                "detail": "creator lookup reachable" if lookup_ok else f"creator lookup blocked (HTTP {lookup.status_code})"}
    except Exception as exc:
        return {"ok": False, "detail": f"Weibo API check failed: {type(exc).__name__}: {exc}"}


def _run_login(project_root: Path, python: Path) -> dict[str, Any]:
    login = project_root / "scripts" / "douyin_login.ps1"
    if not login.is_file():
        return {"ok": False, "detail": "douyin_login.ps1 is missing"}
    rc, out, err = _run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(login), "-Python", str(python)], max(60, int(os.getenv("PREFLIGHT_LOGIN_TIMEOUT_SECONDS", "600"))))
    return {"ok": rc == 0, "returncode": rc, "detail": (err or out)[-1200:]}


def _looks_like_auth_failure(check: dict[str, Any]) -> bool:
    detail = str(check.get("detail") or "").lower()
    return any(marker in detail for marker in ("登录", "未检测到抖音登录态", "authrequired", "unauthorized", "401", "expired"))


def _persistent_login_needed(cookie_state: str | None) -> bool:
    """Only local session state can trigger an automatic QR login."""
    return cookie_state in {"missing", "invalid", "missing_auth", "expired"}


def _douyin_health_check_enabled() -> bool:
    """Live platform traffic is opt-in; startup should normally stay local."""
    return os.getenv("PREFLIGHT_DOUYIN_HEALTH_CHECK", "false").lower() in {"1", "true", "yes"}


def _douyin_startup_ready(bridge: dict[str, Any], cookie: dict[str, Any]) -> bool:
    return bool(bridge.get("ok") and cookie.get("state") == "valid")


def _check_or_defer_douyin(project_root: Path, python: Path, checks: dict[str, Any]) -> dict[str, Any]:
    prerequisites = all(checks[name].get("ok") for name in ("python", "imports", "browser", "bridge", "dy_dependencies"))
    if not prerequisites or checks["cookie"].get("state") != "valid":
        return {"ok": False, "skipped": True, "detail": "local prerequisites or persistent login state are incomplete"}
    if _douyin_health_check_enabled():
        return _check_douyin_health(project_root, python)
    return {
        "ok": True,
        "skipped": True,
        "deferred": True,
        "detail": "persistent login state is valid; live platform check deferred until the first crawl",
    }


def run_preflight(project_root: Path, python: Path, *, auto_repair: bool = True, require_sources: bool = False) -> dict[str, Any]:
    project_root, python = project_root.resolve(), python.resolve()
    try:
        from dotenv import load_dotenv
        load_dotenv(project_root / ".env", override=False)
    except ImportError:
        pass
    cache_dir = project_root / ".cache"
    (project_root / "downloads").mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    checks: dict[str, Any] = {}
    report: dict[str, Any] = {"ok": True, "started_at": time.time(), "checks": checks, "repairs": []}
    checks["python"] = {"ok": python.is_file(), "path": str(python)}
    checks["imports"] = _check_imports(python) if checks["python"]["ok"] else {"ok": False, "detail": "Python executable is missing"}
    checks["bridge"] = _check_bridge(project_root)
    checks["dy_dependencies"] = _check_douyin_imports(project_root, python) if checks["bridge"].get("ok") else {"ok": False, "detail": "dy-cli source missing or incomplete"}
    checks["cookie"] = inspect_cookie_file(cookie_file_path())
    checks["browser"] = _check_browser(python) if checks["imports"].get("ok") else {"ok": False, "detail": "imports are unavailable"}
    checks["weibo_bridge"] = _check_weibo_bridge(project_root, python) if checks["python"]["ok"] else {"ok": False, "detail": "Python executable is missing"}
    checks["xhs_bridge"] = _check_xhs_bridge(project_root, python) if checks["python"]["ok"] else {"ok": False, "detail": "Python executable is missing"}
    checks["gallery"] = _check_gallery(python) if checks["python"]["ok"] else {"ok": False, "detail": "Python executable is missing"}
    checks["bilibili_bridge"] = _check_bilibili_bridge(project_root, python) if checks["python"]["ok"] else {"ok": False, "detail": "Python executable is missing"}
    live = os.getenv("PREFLIGHT_LIVE_CHECKS", "false").lower() in {"1", "true", "yes"}
    checks["weibo_api"] = _check_weibo_api() if live and checks["imports"].get("ok") else {"ok": False, "skipped": True, "deferred": True, "detail": "Browser session and live access checked during collection"}
    checks["douyin"] = _check_or_defer_douyin(project_root, python, checks)

    if auto_repair and not checks["imports"].get("ok") and checks["python"].get("ok"):
        rc, out, err = _run([str(python), "-m", "pip", "install", "-e", f"{project_root}[browser]"], 180)
        report["repairs"].append({"action": "install desktop dependencies", "ok": rc == 0, "detail": (err or out)[-1200:]})
        if rc == 0:
            checks["imports"] = _check_imports(python)
            checks["browser"] = _check_browser(python) if checks["imports"].get("ok") else {"ok": False, "detail": "imports remain unavailable"}

    if auto_repair and checks["imports"].get("ok") and not checks["browser"].get("ok"):
        rc, out, err = _run([str(python), "-m", "playwright", "install", "chromium"], max(30, int(os.getenv("PREFLIGHT_BROWSER_INSTALL_TIMEOUT_SECONDS", "120"))))
        report["repairs"].append({"action": "install Playwright Chromium", "ok": rc == 0, "detail": (err or out)[-1200:]})
        if rc == 0:
            checks["browser"] = _check_browser(python)

    if auto_repair and checks["weibo_bridge"].get("configured") and not checks["weibo_bridge"].get("ok"):
        requirements = project_root / "scripts" / "requirements_media_crawler_bridge.txt"
        rc, out, err = _run([str(python), "-m", "pip", "install", "-r", str(requirements)], 300)
        report["repairs"].append({"action": "repair MediaCrawler bridge dependencies", "ok": rc == 0, "detail": (err or out)[-1200:]})
        if rc == 0:
            checks["weibo_bridge"] = _check_weibo_bridge(project_root, python)

    if checks["bridge"].get("configured") and checks["bridge"].get("ok") and checks["douyin"].get("skipped") and all(checks[name].get("ok") for name in ("python", "imports", "browser")):
        checks["douyin"] = _check_or_defer_douyin(project_root, python, checks)

    if auto_repair and checks["bridge"].get("ok") and not checks["dy_dependencies"].get("ok"):
        dy_root = project_root / "third_party" / "dy-cli"
        rc, out, err = _run([str(python), "-m", "pip", "install", "-e", str(dy_root)], 180)
        report["repairs"].append({"action": "repair dy-cli dependencies", "ok": rc == 0, "detail": (err or out)[-1200:]})
        if rc == 0:
            checks["dy_dependencies"] = _check_douyin_imports(project_root, python)
            checks["douyin"] = _check_or_defer_douyin(project_root, python, checks)

    cookie_state = checks["cookie"].get("state")
    # A transient API/verify failure must not force a new QR login when the
    # persisted auth cookies are still structurally valid. Re-login only when
    # the local session file is actually missing, invalid or expired.
    login_needed = _persistent_login_needed(cookie_state)
    if auto_repair and login_needed and checks["bridge"].get("ok") and checks["dy_dependencies"].get("ok") and os.getenv("PREFLIGHT_AUTO_LOGIN", "false").lower() in {"1", "true", "yes"}:
        login = _run_login(project_root, python)
        report["repairs"].append({"action": "refresh Douyin login", **login})
        checks["cookie"] = inspect_cookie_file(cookie_file_path())
        if checks["cookie"].get("state") == "valid" and checks["browser"].get("ok"):
            checks["douyin"] = _check_or_defer_douyin(project_root, python, checks)

    local_ok = all(checks[name].get("ok", False) for name in ("python", "imports"))
    douyin_session_available = _douyin_startup_ready(checks["bridge"], checks["cookie"])
    # A live API challenge is platform state, not a desktop startup failure.
    # Keep other platforms and the local UI usable whenever the persisted
    # session and local bridge are structurally ready.
    report["installation_ready"] = all(checks[name].get("ok", False) for name in ("python", "imports", "browser", "bridge", "dy_dependencies", "weibo_bridge", "xhs_bridge", "gallery", "bilibili_bridge"))
    report["ok"] = report["installation_ready"] if require_sources else local_ok
    report["douyin_session_available"] = douyin_session_available
    report["douyin_ready"] = bool(checks["douyin"].get("ok"))
    report["weibo_bridge_available"] = bool(checks["weibo_bridge"].get("ok"))
    report["weibo_ready"] = bool(checks["weibo_api"].get("creator_lookup_ok"))
    report["bilibili_ready"] = bool(checks["bilibili_bridge"].get("ok"))
    report["platforms"] = platform_states(checks)
    report["finished_at"] = time.time()
    report["report_path"] = str(cache_dir / "preflight-latest.json")
    (cache_dir / "preflight-latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def platform_states(checks: dict[str, Any]) -> dict[str, dict[str, str]]:
    states: dict[str, dict[str, str]] = {}
    for platform, source in (("douyin", "bridge"), ("weibo", "weibo_bridge"), ("xhs", "xhs_bridge"), ("x", "gallery"), ("instagram", "gallery"), ("bilibili", "bilibili_bridge")):
        check = checks[source]
        if not check.get("ok") or (platform == "douyin" and not checks["dy_dependencies"].get("ok")):
            states[platform] = {"state": "missing_or_broken_source", "detail": "采集程序缺失或依赖不完整，请重新运行 安装桌面版.bat。"}
        elif platform in {"douyin", "weibo", "xhs"} and not checks["browser"].get("ok"):
            states[platform] = {"state": "browser_unavailable", "detail": "浏览器不可用，请安装 Edge 或重新运行 安装桌面版.bat。"}
        elif platform == "douyin" and checks["cookie"].get("state") != "valid":
            states[platform] = {"state": "login_required", "detail": "首次使用或登录态失效，请双击 登录抖音.bat；其它平台可以继续使用。"}
        else:
            states[platform] = {"state": "ready_unverified", "detail": "采集程序就绪；账号及接口可用性将在该平台实际采集时核验。"}
    return states


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Check and repair desktop crawler startup dependencies")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument("--require-sources", action="store_true", help="Fail installation if any local CLI source or dependency is missing; no account login required")
    args = parser.parse_args()
    report = run_preflight(args.project_root, args.python, auto_repair=not args.no_repair, require_sources=args.require_sources)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        print("启动前自检未通过：请按上面的检查结果修复后重试。", file=sys.stderr)
        return 1
    for platform, status in report["platforms"].items():
        if status["state"] != "ready_unverified":
            print(f"提示：{platform}：{status['detail']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
