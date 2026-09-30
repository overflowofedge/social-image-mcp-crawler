from __future__ import annotations

"""Small local web application for running social-image-mcp without Codex."""

import argparse
import asyncio
import json
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from social_image_mcp.intent import parse_intent
from social_image_mcp.server import search_images, service

HTML = (ROOT / "scripts" / "app.html").read_text(encoding="utf-8")
APP_ID = "social-image-mcp-desktop"
PREFLIGHT_REPORT = ROOT / ".cache" / "preflight-latest.json"


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows permits multiple HTTPServer instances on one port when
    # SO_REUSEADDR is enabled, allowing requests to reach an older process.
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _is_running_app(url: str) -> bool:
    # Bypass system proxies for this local instance check. A matching folder
    # prevents accidentally reopening a different checkout of the application.
    opener = build_opener(ProxyHandler({}))
    for attempt in range(3):
        try:
            with opener.open(url + "api/health", timeout=1) as response:
                payload = json.loads(response.read(8192))
            return (
                isinstance(payload, dict)
                and payload.get("app") == APP_ID
                and payload.get("root") == str(ROOT)
            )
        except (OSError, ValueError):
            # A simultaneous first launch may have bound its socket before
            # it starts answering HTTP requests.
            if attempt < 2:
                time.sleep(0.2)
    return False


def _preflight_report() -> dict:
    try:
        payload = json.loads(PREFLIGHT_REPORT.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _preview_referer(image_url: str, referer: str = "") -> str:
    """Choose a page referer for image hosts that reject direct hotlinks."""
    if referer.startswith(("http://", "https://")):
        return referer
    host = (urlparse(image_url).hostname or "").lower()
    if host.endswith("sinaimg.cn") or host.endswith("weibo.cn") or host.endswith("weibo.com"):
        return "https://m.weibo.cn/"
    if host.endswith("douyinpic.com") or host.endswith("douyincdn.com"):
        return "https://www.douyin.com/"
    return ""


async def _fetch_preview(image_url: str, referer: str = "") -> tuple[str, bytes]:
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("preview URL must be an http(s) image URL")
    if service.client is None:
        raise RuntimeError("preview client is not initialized")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if chosen := _preview_referer(image_url, referer):
        headers["Referer"] = chosen
    response = await service.client.get(image_url, headers=headers, follow_redirects=True, timeout=30)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not content_type.startswith("image/"):
        raise ValueError(f"preview response is not an image: {content_type or 'unknown content type'}")
    return content_type, response.content

class _Loop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(service.start())
        self.loop.run_forever()

    def call(self, coroutine, timeout: float = 300):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=timeout)

    def close(self) -> None:
        future = asyncio.run_coroutine_threadsafe(service.close(), self.loop)
        try:
            future.result(timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)


class Handler(BaseHTTPRequestHandler):
    runner: _Loop

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8") -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self._send(200, (ROOT / "scripts" / "app.js").read_text(encoding="utf-8"), "application/javascript; charset=utf-8")
        elif parsed.path == "/api/health":
            self._send(200, {"app": APP_ID, "root": str(ROOT), "preflight": _preflight_report()})
        elif parsed.path == "/api/status":
            self._send(200, {"platforms": service.statuses(), "sources": service.source_statuses(), "preflight": _preflight_report()})
        elif parsed.path == "/api/image":
            query = parse_qs(parsed.query)
            image_url = str((query.get("url") or [""])[0])
            referer = str((query.get("referer") or [""])[0])
            try:
                content_type, data = self.runner.call(_fetch_preview(image_url, referer), timeout=45)
                self._send_bytes(200, data, content_type)
            except Exception as exc:
                self._send(502, {"error": f"image preview failed: {exc}"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/search":
            self._send(404, {"error": "not found"}); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or not str(body.get("query") or "").strip():
                raise ValueError("请输入搜索提示词或链接")
            query = str(body["query"])
            platforms = body.get("platforms") or None
            detected = parse_intent(query).identifier_platform
            # A pasted platform URL is authoritative. This prevents the
            # default Douyin checkbox from accidentally routing an X/Weibo URL
            # to the wrong adapter.
            if detected and platforms and detected not in platforms:
                platforms = None
            selected_platform = platforms[0] if platforms and len(platforms) == 1 else None
            # The checkbox is intentionally gone: short, single-platform
            # names are first tried as exact creator names. If no account is
            # found, the same input is retried as a normal keyword search.
            auto_creator = None
            auto_creator_id = None
            if not detected and selected_platform in {"douyin", "weibo", "x"}:
                compact = query.strip()
                looks_like_name = (
                    len(compact) <= 24
                    and " " not in compact
                    and "\n" not in compact
                    and not compact.startswith(("http://", "https://"))
                )
                if looks_like_name:
                    has_chinese = any("\u4e00" <= char <= "\u9fff" for char in compact)
                    if compact.isdigit() or (selected_platform in {"douyin", "x"} and not has_chinese):
                        auto_creator_id = compact.lstrip("@")
                    else:
                        auto_creator = compact.lstrip("@")
            def optional_int(name):
                value = body.get(name)
                return int(value) if value not in (None, "", 0, "0") else None
            search_kwargs = dict(
                query=query, platforms=platforms,
                max_results=int(body.get("max_results", 20)),
                media_type=str(body.get("media_type", "images")),
                image_limit=optional_int("image_limit"), video_limit=optional_int("video_limit"),
                per_post_limit=optional_int("per_post_limit"), max_posts=int(body.get("max_posts", 20)),
                download=bool(body.get("download", True)), content_query=body.get("content_query") or None,
                filter_mode=str(body.get("filter_mode", "off")), quality_mode=str(body.get("quality_mode", "fast")),
                retrieval_mode="sources", use_cache=False,
            )
            result = self.runner.call(search_images(**search_kwargs, creator_name=auto_creator, creator_id=auto_creator_id))
            error = result.get("error") if isinstance(result, dict) else None
            if (auto_creator or auto_creator_id) and isinstance(error, dict):
                message = str(error.get("message", "")).lower()
                if any(marker in message for marker in ("matched 0", "not found", "invalid creator")):
                    result = self.runner.call(search_images(**search_kwargs, creator_name=None, creator_id=None))
            self._send(200, result)
        except Exception as exc:
            self._send(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8765); parser.add_argument("--no-browser", action="store_true"); args = parser.parse_args()
    browser_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{browser_host}:{args.port}/"
    try:
        server = LocalHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if _is_running_app(url):
            print(f"应用已在运行：{url}", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return
        raise SystemExit(
            f"无法启动应用，端口 {args.port} 不可用。请关闭占用该端口的程序，"
            f"或使用 scripts/start_app.ps1 -Port {args.port + 1} 指定其他端口。\n{exc}"
        ) from None
    runner = _Loop(); Handler.runner = runner
    print(f"社交媒体采集器已启动：{url}", flush=True)
    if not args.no_browser: threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); runner.close()

if __name__ == "__main__":
    main()
