"""Official platform login with automatic session capture, usable by UI or CLI."""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from social_image_mcp.accounts import PLATFORMS, QR_PLATFORMS, cookie_header, has_auth, read_session, save_session

LOGIN_URLS = {
    "douyin": "https://creator.douyin.com/",
    "weibo": "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog",
    "xhs": "https://www.xiaohongshu.com/explore",
    "bilibili": "https://passport.bilibili.com/login",
    "x": "https://x.com/i/flow/login",
    "instagram": "https://www.instagram.com/accounts/login/",
}
ACTIVE_STATES = {"opening", "waiting", "verifying", "saving", "cancelling"}
LOGIN_PAGE_HOSTS = {
    "douyin": {"douyin.com"},
    "x": {"x.com", "twitter.com"},
}


def _page_belongs_to_login(platform: str, page) -> bool:
    """Return whether a popup can be part of this platform's login flow."""
    url = str(getattr(page, "url", "") or "")
    if not url:
        return True
    hostname = (urlparse(url).hostname or "").lower().lstrip(".")
    return any(hostname == domain or hostname.endswith("." + domain)
               for domain in LOGIN_PAGE_HOSTS.get(platform, set()))


async def _weibo_authenticated(context) -> bool:
    # Check the desktop account endpoint first. Do not open a second mobile
    # login page just to obtain a second cookie: the SSO session is shared by
    # the crawler and the browser context already carries all returned cookies.
    for url in ("https://weibo.com/ajax/config", "https://m.weibo.cn/api/config"):
        try:
            response = await context.request.get(url, timeout=5000)
            if response.status != 200:
                continue
            payload = await response.json()
            data = payload.get("data", {})
            if isinstance(data, dict) and data.get("login") in (True, 1, "1"):
                return True
        except Exception:
            continue
    return False


async def _bilibili_authenticated(context) -> bool:
    try:
        response = await context.request.get("https://api.bilibili.com/x/web-interface/nav", timeout=5000)
        if response.status != 200:
            return False
        payload = await response.json()
        return payload.get("code") == 0 and payload.get("data", {}).get("isLogin") is True
    except Exception:
        return False


def _xhs_account_response(payload: dict) -> bool:
    if not isinstance(payload, dict) or payload.get("success") is False:
        return False
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("guest") is True:
        return False
    result = data.get("result")
    if isinstance(result, dict) and result.get("success") is True:
        return True
    return (data.get("guest") is False and bool(data.get("user_id") or data.get("userid")))


async def _xhs_authenticated(context, confirmed: bool) -> bool:
    if confirmed:
        return True
    # A guest may also have web_session. Require official account information
    # or the signed-in profile link used by the website itself.
    for page in context.pages:
        try:
            selector = 'xpath=//a[contains(@href, "/user/profile/")]//span[text()="我"]'
            if await page.locator(selector).first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _login_browser(platform: str, root: Path, cancel: threading.Event, update,
                         timeout: float = 300) -> None:
    async def watch_cancel():
        while not cancel.is_set():
            await asyncio.sleep(0.2)

    login = asyncio.create_task(_login_browser_session(platform, root, cancel, update, timeout))
    watch = asyncio.create_task(watch_cancel())
    try:
        done, _ = await asyncio.wait({login, watch}, return_when=asyncio.FIRST_COMPLETED)
        if login in done:
            await login
        else:
            raise asyncio.CancelledError()
    finally:
        for task in (login, watch):
            if not task.done():
                task.cancel()
        await asyncio.gather(login, watch, return_exceptions=True)


async def _login_browser_session(platform: str, root: Path, cancel: threading.Event, update,
                                 timeout: float) -> None:
    from playwright.async_api import async_playwright
    from scripts.preflight import _browser_channel

    update("opening", "正在打开官方登录窗口…")
    async with async_playwright() as playwright:
        options = {"headless": False}
        channel = _browser_channel()
        if channel:
            options["channel"] = channel
        browser = await playwright.chromium.launch(**options)
        context = None
        try:
            # A fresh context ensures '重新登录' can select a different account.
            # Do not use or alter the user's regular browser profile.
            context = await browser.new_context()
            xhs_confirmed = False

            async def observe_xhs(response):
                nonlocal xhs_confirmed
                parsed = urlparse(response.url)
                if (parsed.hostname == "edith.xiaohongshu.com"
                        and parsed.path in {"/api/sns/web/v1/user/selfinfo", "/api/sns/web/v2/user/me"}
                        and response.status == 200):
                    try:
                        payload = await response.json()
                        if isinstance(payload, dict) and _xhs_account_response(payload):
                            xhs_confirmed = True
                    except Exception:
                        pass

            if platform == "xhs":
                context.on("response", observe_xhs)
            pending_pages = []

            def observe_login_page(new_page):
                if new_page not in pending_pages:
                    pending_pages.append(new_page)

            # Some official login flows open a popup instead of navigating the
            # original tab. Keep the newest platform page and close the old
            # one so the user sees one stable login window.
            if platform in LOGIN_PAGE_HOSTS and hasattr(context, "on"):
                context.on("page", observe_login_page)
            page = await context.new_page()
            await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded", timeout=45000)
            if platform in {"douyin", "xhs"}:
                for text in ("登录", "扫码登录"):
                    try:
                        await page.get_by_text(text, exact=True).first.click(timeout=2500)
                        # The first click opens the platform's login panel. A
                        # second click immediately switches panels and makes
                        # the official QR page visibly flash.
                        break
                    except Exception:
                        pass
            message = (f"请使用{PLATFORMS[platform]} App 扫码，并在手机上确认登录。成功后会自动保存。"
                       if platform in QR_PLATFORMS else
                       f"请在官方窗口完成 {PLATFORMS[platform]} 登录及验证，成功后会自动保存。")
            update("waiting", message)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                if not browser.is_connected() or not context.pages:
                    raise asyncio.CancelledError()
                while pending_pages:
                    candidate = pending_pages.pop(0)
                    if candidate is page:
                        continue
                    if not _page_belongs_to_login(platform, candidate):
                        try:
                            await candidate.close()
                        except Exception:
                            pass
                        continue
                    previous = page
                    page = candidate
                    try:
                        await previous.close()
                    except Exception:
                        pass
                state = await context.storage_state()
                if has_auth(platform, state):
                    if platform == "douyin":
                        web_cookie = cookie_header("douyin", await context.storage_state(), "https://www.douyin.com/")
                        if not any(part.strip().split("=", 1)[0] in {"sessionid", "sessionid_ss"}
                                   for part in web_cookie.split(";")):
                            update("verifying", "正在同步抖音登录状态，请完成窗口中的平台验证。")
                            await asyncio.sleep(0.5)
                            continue
                    if platform == "xhs" and not await _xhs_authenticated(context, xhs_confirmed):
                        # Keep the guest session in memory until login is
                        # confirmed. Do not persist it or close the QR window.
                        await asyncio.sleep(0.5)
                        continue
                    if cancel.is_set():
                        raise asyncio.CancelledError()
                    update("saving", "登录已确认，正在保存到本机…")
                    save_session(platform, await context.storage_state(), root)
                    return
                await asyncio.sleep(0.5)
            raise TimeoutError("登录超时，请点击登录重试；已有登录信息会保留。")
        finally:
            try:
                if context:
                    await context.close()
            finally:
                await browser.close()


class LoginManager:
    """One cancellable login per platform, independent of collection workers."""

    def __init__(self, root: Path = ROOT, login=_login_browser, on_success=None):
        self.root = root
        self.login = login
        self.on_success = on_success
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    def status(self, platform: str) -> dict:
        if platform not in PLATFORMS:
            raise ValueError("请选择支持的平台登录；普通网页无需平台账号。")
        with self._lock:
            saved = has_auth(platform, read_session(platform, self.root))
            job = self._jobs.get(platform)
            data = dict(job) if job else {
                "platform": platform, "state": "logged_in" if saved else "login_required",
                "message": "已保存登录状态，可直接采集。" if saved else (
                    "首次使用请点击扫码登录。" if platform in QR_PLATFORMS else "首次使用请点击网页登录。"),
            }
            if not saved and data["state"] == "completed":
                data.update(state="login_required", message="保存的登录状态缺失或已过期，请重新登录。")
            data["login_label"] = "扫码登录" if platform in QR_PLATFORMS else "网页登录"
            data["session_available"] = saved
            data["active"] = data["state"] in ACTIVE_STATES
            return data

    def statuses(self) -> list[dict]:
        return [self.status(platform) for platform in PLATFORMS]

    def start(self, platform: str, force: bool = False) -> dict:
        with self._lock:
            current = self.status(platform)
            if current["active"] or (current["session_available"] and not force):
                return current
            task_id = uuid.uuid4().hex
            self._jobs[platform] = {"task_id": task_id, "platform": platform,
                                    "state": "opening", "message": "正在启动官方登录…"}
            cancel = threading.Event()
            self._cancel[platform] = cancel
            thread = threading.Thread(target=self._run, args=(platform, task_id, cancel), daemon=True)
            self._threads[platform] = thread
            thread.start()
            return self.status(platform)

    def _update(self, platform: str, task_id: str, state: str, message: str) -> None:
        with self._lock:
            if self._jobs[platform]["task_id"] == task_id:
                if self._jobs[platform]["state"] == "cancelling" and state in ACTIVE_STATES:
                    return
                self._jobs[platform].update(state=state, message=message)

    def _run(self, platform: str, task_id: str, cancel: threading.Event) -> None:
        update = lambda state, message: self._update(platform, task_id, state, message)
        try:
            asyncio.run(self.login(platform, self.root, cancel, update))
            if not has_auth(platform, read_session(platform, self.root)):
                raise RuntimeError("登录信息未保存，请重试登录。")
            if self.on_success:
                self.on_success(platform)
            update("completed", "登录成功，已自动保存。现在可以直接采集，无需重启。")
        except asyncio.CancelledError:
            update("cancelled", "登录已取消，已有登录信息会保留。")
        except TimeoutError as exc:
            update("timeout", str(exc))
        except Exception as exc:
            if cancel.is_set():
                update("cancelled", "登录已取消，已有登录信息会保留。")
            else:
                # Never expose browser/network exception text (it can contain
                # authentication URLs or credentials) through the public API.
                message = ("缺少浏览器组件，请重新运行 安装桌面版.bat。"
                           if isinstance(exc, ImportError) else
                           "无法完成登录。请检查网络、浏览器和平台验证后重试；已有登录信息会保留。")
                update("failed", message)

    def cancel(self, platform: str) -> dict:
        with self._lock:
            current = self.status(platform)
            if current["active"] and current["state"] != "saving":
                self._cancel[platform].set()
                self._jobs[platform].update(state="cancelling", message="正在关闭登录窗口…")
            return self.status(platform)

    def close(self) -> None:
        with self._lock:
            for cancel in self._cancel.values():
                cancel.set()
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    try:
        asyncio.run(_login_browser(args.platform, ROOT, threading.Event(),
                                  lambda _state, message: print(message, flush=True)))
        print("登录成功，已自动保存。", flush=True)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("登录已取消，已有登录信息会保留。")
    except Exception:
        print("登录未完成，请检查网络、浏览器及平台验证后重试。")
    return 1


if __name__ == "__main__":
    # Direct CLI invocation needs the project root for scripts.preflight.
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
