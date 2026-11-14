"""Site login for custom webpages ("其他平台").

Many forums and gallery sites show their full content only to logged-in
members. This module keeps one Playwright storage state per site domain: the
user logs in inside a visible browser window once, the cookies/localStorage
are saved under ``.cache/web-sessions/<domain>.json``, and the webpage
extractor attaches that state to every render of the same domain afterwards.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def web_session_path(root: Path, url: str) -> Path | None:
    """Map a site URL to its storage-state file, or None for non-web input."""
    candidate = url if "://" in url else f"https://{url}"
    parsed = urlparse(candidate)
    netloc = (parsed.netloc or "").lower()
    if not netloc or parsed.scheme not in {"http", "https"}:
        return None
    safe = re.sub(r"[^a-z0-9.\-]+", "_", netloc)[:80] or "site"
    return Path(root) / ".cache" / "web-sessions" / f"{safe}.json"


def _launch_options() -> dict[str, Any]:
    options: dict[str, Any] = {"headless": False}  # the user must interact
    executable = os.getenv("DOUYIN_BROWSER_PATH") or os.getenv("MEDIA_CRAWLER_BROWSER_PATH")
    if executable and Path(executable).is_file():
        options["executable_path"] = executable
    elif os.getenv("DOUYIN_BROWSER_CHANNEL") or os.getenv("BROWSER_CHANNEL"):
        options["channel"] = os.getenv("DOUYIN_BROWSER_CHANNEL") or os.getenv("BROWSER_CHANNEL")
    return options


def _atomic_save(context: Any, session: Path) -> bool:
    temporary = session.with_suffix(session.suffix + ".tmp")
    try:
        context.storage_state(path=str(temporary))
        os.replace(temporary, session)
        return True
    except Exception:
        return False


class WebLoginManager:
    """One interactive site login at a time; closing the window saves it."""

    def __init__(self, root: Path = ROOT, login: Callable | None = None, timeout_seconds: float | None = None):
        self.root = Path(root)
        self.login = login or self._login_window
        self.timeout = float(timeout_seconds or os.getenv("WEB_LOGIN_TIMEOUT_SECONDS", "300"))
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._cancel: dict[str, threading.Event] = {}

    def status(self, url: str) -> dict:
        session = web_session_path(self.root, url)
        if session is None:
            return {"ok": False, "state": "invalid",
                    "message": "请先在输入框填写完整 http(s) 网址，再登录站点。"}
        with self._lock:
            job = self._jobs.get(session.stem)
            data = dict(job) if job else {
                "state": "saved" if session.is_file() else "login_required",
                "message": "已保存站点登录状态。" if session.is_file() else "这个站点还没有保存登录状态。",
            }
        data["ok"] = True
        data["session_available"] = session.is_file()
        data["domain"] = session.stem
        return data

    def start(self, url: str) -> dict:
        session = web_session_path(self.root, url)
        if session is None:
            return {"ok": False, "state": "invalid",
                    "message": "请先在输入框填写完整 http(s) 网址，再登录站点。"}
        with self._lock:
            job = self._jobs.get(session.stem)
            if job and job.get("state") in {"opening", "waiting", "saving"}:
                return dict(job)
            task_id = uuid.uuid4().hex
            cancel = threading.Event()
            self._cancel[session.stem] = cancel
            self._jobs[session.stem] = {"state": "opening", "message": "正在打开浏览器…",
                                        "url": url, "task_id": task_id}
            thread = threading.Thread(target=self._run, args=(url, session, cancel), daemon=True)
            thread.start()
            return self.status(url)

    def _update(self, key: str, state: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                job.update(state=state, message=message)

    def _run(self, url: str, session: Path, cancel: threading.Event) -> None:
        key = session.stem

        def update(state: str, message: str) -> None:
            self._update(key, state, message)

        try:
            asyncio.run(self.login(url, session, cancel, update, self.timeout))
            if session.is_file():
                update("saved", "站点登录状态已保存。重新运行采集即可获取登录后的内容。")
            else:
                update("failed", "未检测到登录变化；请在浏览器窗口完成登录后再关闭它。")
        except Exception as exc:
            update("failed", f"站点登录失败：{exc}")

    async def _login_window(self, url: str, session: Path, cancel: threading.Event,
                            update: Callable[[str, str], None], timeout: float) -> None:
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            if exc.name not in {"playwright", "playwright.async_api"}:
                raise
            raise RuntimeError("浏览器采集依赖 Playwright 未安装；请重新运行 安装桌面版.bat。") from exc

        session.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**_launch_options())
            context_options: dict[str, Any] = {"viewport": {"width": 1440, "height": 1000}}
            if session.is_file():
                context_options["storage_state"] = str(session)
            context = await browser.new_context(**context_options)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                update("waiting", f"站点页面加载较慢或被拦（{str(exc)[:60]}）；仍可在窗口中继续操作。")
            update("waiting", "请在浏览器窗口中登录站点；完成后直接关闭浏览器窗口，登录状态会自动保存。")
            deadline = time.monotonic() + timeout
            last_save = 0.0
            while time.monotonic() < deadline and not cancel.is_set():
                await asyncio.sleep(2)
                if not browser.is_connected() or not context.pages:
                    break
                if time.monotonic() - last_save >= 2:
                    _atomic_save(context, session)
                    last_save = time.monotonic()
            _atomic_save(context, session)
            try:
                await browser.close()
            except Exception:
                pass
