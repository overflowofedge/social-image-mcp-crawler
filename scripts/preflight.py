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


def _run(command: list[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)
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
    rc, out, err = _run([str(python), "-c", "import httpx, mcp, PIL, playwright; print('ok')"], 20)
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
    ok = bridge.is_file() and source_root.is_dir()
    return {"ok": ok, "configured": source_root.is_dir(), "bridge": str(bridge), "source_root": str(source_root), "detail": "ready" if ok else "抖音来源未安装，抖音功能不可用"}


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


def _run_login(project_root: Path, python: Path) -> dict[str, Any]:
    login = project_root / "scripts" / "douyin_login.ps1"
    if not login.is_file():
        return {"ok": False, "detail": "douyin_login.ps1 is missing"}
    rc, out, err = _run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(login), "-Python", str(python)], max(60, int(os.getenv("PREFLIGHT_LOGIN_TIMEOUT_SECONDS", "600"))))
    return {"ok": rc == 0, "returncode": rc, "detail": (err or out)[-1200:]}


def _looks_like_auth_failure(check: dict[str, Any]) -> bool:
    detail = str(check.get("detail") or "").lower()
    return any(marker in detail for marker in ("登录", "未检测到抖音登录态", "authrequired", "unauthorized", "401", "expired"))


def run_preflight(project_root: Path, python: Path, *, auto_repair: bool = True) -> dict[str, Any]:
    project_root, python = project_root.resolve(), python.resolve()
    cache_dir = project_root / ".cache"
    (project_root / "downloads").mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    checks: dict[str, Any] = {}
    report: dict[str, Any] = {"ok": True, "started_at": time.time(), "checks": checks, "repairs": []}
    checks["python"] = {"ok": python.is_file(), "path": str(python)}
    checks["imports"] = _check_imports(python) if checks["python"]["ok"] else {"ok": False, "detail": "Python executable is missing"}
    checks["bridge"] = _check_bridge(project_root)
    checks["cookie"] = inspect_cookie_file(cookie_file_path())
    checks["browser"] = _check_browser(python) if checks["imports"].get("ok") else {"ok": False, "detail": "imports are unavailable"}
    if all(checks[name].get("ok") for name in ("python", "imports", "browser", "bridge")):
        checks["douyin"] = _check_douyin_health(project_root, python)
    else:
        checks["douyin"] = {"ok": False, "skipped": True, "detail": "local prerequisites are incomplete"}

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

    if checks["bridge"].get("configured") and checks["bridge"].get("ok") and checks["douyin"].get("skipped") and all(checks[name].get("ok") for name in ("python", "imports", "browser")):
        checks["douyin"] = _check_douyin_health(project_root, python)

    if auto_repair and checks["bridge"].get("configured") and not checks["douyin"].get("ok"):
        detail = str(checks["douyin"].get("detail") or "").lower()
        if "no module named" in detail or "modulenotfound" in detail or "importerror" in detail:
            dy_root = project_root / "third_party" / "dy-cli"
            rc, out, err = _run([str(python), "-m", "pip", "install", "-e", str(dy_root)], 180)
            report["repairs"].append({"action": "repair dy-cli dependencies", "ok": rc == 0, "detail": (err or out)[-1200:]})
            if rc == 0:
                checks["douyin"] = _check_douyin_health(project_root, python)

    cookie_state = checks["cookie"].get("state")
    login_needed = cookie_state in {"missing", "invalid", "missing_auth", "expired"} or _looks_like_auth_failure(checks["douyin"])
    if auto_repair and login_needed and checks["bridge"].get("ok") and os.getenv("PREFLIGHT_AUTO_LOGIN", "true").lower() in {"1", "true", "yes"}:
        login = _run_login(project_root, python)
        report["repairs"].append({"action": "refresh Douyin login", **login})
        checks["cookie"] = inspect_cookie_file(cookie_file_path())
        if checks["cookie"].get("state") == "valid" and checks["browser"].get("ok"):
            checks["douyin"] = _check_douyin_health(project_root, python)

    local_ok = all(checks[name].get("ok", False) for name in ("python", "imports", "browser"))
    douyin_configured = bool(checks["bridge"].get("configured"))
    report["ok"] = local_ok and (not douyin_configured or (checks["bridge"].get("ok") and checks["cookie"].get("state") == "valid" and checks["douyin"].get("ok")))
    report["douyin_ready"] = bool(checks["douyin"].get("ok"))
    report["finished_at"] = time.time()
    report["report_path"] = str(cache_dir / "preflight-latest.json")
    (cache_dir / "preflight-latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


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
    args = parser.parse_args()
    report = run_preflight(args.project_root, args.python, auto_repair=not args.no_repair)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        print("启动前自检未通过：请按上面的检查结果修复后重试。", file=sys.stderr)
        return 1
    if not report["douyin_ready"]:
        print("提示：抖音链路动态检查未通过，请查看 preflight-latest.json。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
