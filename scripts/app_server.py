from __future__ import annotations

"""Small local web application for running social-image-mcp without Codex."""

import argparse
import asyncio
import json
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from social_image_mcp.intent import parse_intent
from social_image_mcp.progress import progress_context
from social_image_mcp.server import search_images, service

HTML = (ROOT / "scripts" / "app.html").read_text(encoding="utf-8")
APP_ID = "social-image-mcp-desktop"
PREFLIGHT_REPORT = ROOT / ".cache" / "preflight-latest.json"


class TaskRegistry:
    """Thread-safe, bounded storage for desktop task progress."""

    _COUNTERS = {
        "pages_fetched", "posts_fetched", "images_found", "videos_found",
        "download_completed", "download_total", "downloaded", "existing",
        "duplicate", "rejected", "failed",
    }

    def __init__(self, limit: int = 100) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}

    def _snapshot(self, task: dict) -> dict:
        now = task.get("_finished_monotonic") or time.monotonic()
        started = task.get("_started_monotonic") or task["_created_monotonic"]
        public = {key: value for key, value in task.items() if not key.startswith("_")}
        public["elapsed_seconds"] = round(max(0.0, now - started), 1)
        public["activity"] = [dict(entry) for entry in task.get("activity", [])]
        return public

    def create(self) -> dict:
        task_id = uuid.uuid4().hex
        now = time.monotonic()
        task = {
            "task_id": task_id, "state": "queued", "stage": "queued",
            "message": "任务已排队，正在启动后台采集。", "activity": [],
            "warnings": [], "stop_reason": None, "result": None,
            "_created_monotonic": now, "_started_monotonic": None,
            "_finished_monotonic": None,
            **{name: 0 for name in self._COUNTERS},
        }
        with self._lock:
            if len(self._tasks) >= self.limit:
                finished = sorted(
                    (value for value in self._tasks.values() if value["state"] in {"completed", "failed"}),
                    key=lambda value: value["_created_monotonic"],
                )
                for old in finished[: max(1, len(self._tasks) - self.limit + 1)]:
                    self._tasks.pop(old["task_id"], None)
            self._tasks[task_id] = task
            self._append_activity(task, task["message"], "info")
            return self._snapshot(task)

    @staticmethod
    def _append_activity(task: dict, message: str, level: str) -> None:
        if not message:
            return
        elapsed = 0.0
        if task.get("_started_monotonic"):
            elapsed = time.monotonic() - task["_started_monotonic"]
        last = task["activity"][-1] if task["activity"] else None
        if last and last.get("message") == message:
            return
        task["activity"].append({
            "level": level if level in {"info", "success", "warning", "error"} else "info",
            "message": message,
            "elapsed_seconds": round(max(0.0, elapsed), 1),
        })
        del task["activity"][:-40]

    def start(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task["state"] = "running"
            task["stage"] = "preparing"
            task["message"] = "后台任务已启动，正在准备采集。"
            task["_started_monotonic"] = time.monotonic()
            self._append_activity(task, task["message"], "info")

    def update(self, task_id: str, event: dict) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["state"] in {"completed", "failed"}:
                return
            if event.get("stage"):
                task["stage"] = str(event["stage"])
            if event.get("message"):
                task["message"] = str(event["message"])
            for name in self._COUNTERS:
                if name in event and event[name] is not None:
                    try:
                        task[name] = max(0, int(event[name]))
                    except (TypeError, ValueError):
                        pass
            if event.get("stop_reason"):
                task["stop_reason"] = str(event["stop_reason"])
            warning = event.get("warning")
            if warning and str(warning) not in task["warnings"]:
                task["warnings"].append(str(warning))
            self._append_activity(task, task["message"], str(event.get("level") or "info"))

    def finish(self, task_id: str, result: dict, failed: bool = False) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task["state"] = "failed" if failed else "completed"
            task["stage"] = task["state"]
            task["result"] = result
            task["message"] = "任务失败，详细原因见完成报告。" if failed else "任务已完成。"
            task["_finished_monotonic"] = time.monotonic()
            self._append_activity(task, task["message"], "error" if failed else "success")

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._snapshot(task) if task else None


TASKS = TaskRegistry()


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


def _creator_hint(query: str, selected_platform: str | None, detected: str | None) -> tuple[str | None, str | None]:
    """Infer an account target for the single-platform desktop form."""
    if detected or selected_platform not in {"douyin", "weibo", "bilibili", "x"}:
        return None, None
    compact = query.strip()
    looks_like_name = (
        len(compact) <= 50
        and "\n" not in compact
        and "\r" not in compact
        and not compact.startswith(("http://", "https://"))
    )
    if not looks_like_name:
        return None, None
    # Bilibili account IDs (mid) are numeric. Every other short single-field
    # input is a display nickname, including English names and names with
    # spaces; treating those as IDs makes ``creator_target`` reject them before
    # the account search can run.
    if selected_platform == "bilibili":
        if compact.isdigit():
            return None, compact
        return compact.lstrip("@"), None
    has_chinese = any("\u4e00" <= char <= "\u9fff" for char in compact)
    if compact.isdigit() or (selected_platform in {"douyin", "x"} and not has_chinese):
        return None, compact.lstrip("@")
    return compact.lstrip("@"), None


_PLATFORM_LABELS = {
    "douyin": "抖音", "xhs": "小红书", "weibo": "微博",
    "bilibili": "B站", "x": "X", "instagram": "Instagram", "other": "自定义网页",
}


def _error_guidance(message: str, platform: str) -> tuple[str, str]:
    """Translate source failures into a reason and an actionable next step."""
    raw = str(message or "未知错误").strip()
    lowered = raw.lower()
    label = _PLATFORM_LABELS.get(platform, platform or "当前平台")
    if "请输入搜索提示词或链接" in raw:
        return "没有填写要采集的账号或网址。", "输入账号昵称、完整主页链接或作品链接后再试。"
    if any(marker in lowered for marker in (
        "verify_check", "wbi", "rate limit", "risk control", "验证码",
        "api error -352", "api error -412", "http 412", "http 403", "http 429",
        "temporarily rejected",
    )):
        action = "稍后重试，并确认平台登录状态正常。"
        if platform == "douyin":
            action = "重新运行抖音扫码登录，完成验证后再试；仍失败时稍后重试或更换网络。"
        elif platform == "bilibili":
            action = "稍后重试；频繁出现时，在 .env 中更新 BILIBILI_COOKIE 后重启桌面版。"
        return f"{label}触发了访问频率限制或安全验证，本次请求被平台拒绝。", action
    if any(marker in lowered for marker in ("cookie", "login", "logged in", "登录", "扫码", "unauthorized", "-101")):
        return f"{label}登录状态缺失或已经失效。", "重新完成该平台登录或扫码验证，然后重启桌面版再试。"
    if any(marker in lowered for marker in ("creator_identity_unresolved", "identity_unresolved", "matched 0", "not found", "没有找到")):
        return "没有确认到唯一的账号，昵称可能不准确、存在同名账号，或账号未公开。", "核对完整昵称；仍无法识别时粘贴该账号的完整主页链接。"
    if any(marker in lowered for marker in ("timed out", "timeout", "deadline", "超时")):
        return "平台或下载服务器响应超时。", "先减少下载数量和“最多检索作品数”后重试，并检查网络连接。"
    if any(marker in lowered for marker in ("no image candidates", "no candidates", "returned no", "没有置顶视频", "no video")):
        return "平台没有返回当前类型的可下载内容。", "确认账号或作品为公开状态，并检查“只下载图片/视频”的选择；也可增加检索作品数。"
    if any(marker in lowered for marker in ("below minimum dimensions", "minimum dimensions")):
        return "文件尺寸低于允许的最小值，已跳过。", "降低最小尺寸要求，或选择包含更高清内容的作品。"
    if any(marker in lowered for marker in ("dependency", "module not found", "no module named", "未安装")):
        return "桌面版缺少运行依赖。", "重新运行“安装桌面版.bat”，完成后再启动应用。"
    if any(marker in lowered for marker in ("connection", "connect", "network", "dns", "ssl", "request failed")):
        return "访问平台或媒体服务器时网络连接失败。", "检查网络和代理设置后重试；也可减少单次任务数量。"
    if any(marker in lowered for marker in ("content_filter_required", "required object", "no image matched")):
        return "严格内容筛选没有找到符合条件的内容。", "放宽内容筛选条件，或将筛选模式改为“尽量筛选”。"
    return f"{label}任务未完成：{raw}", "检查账号或网址是否公开、登录状态和网络连接，然后重试。"


def _warning_guidance(message: str, platform: str) -> tuple[str, str]:
    """Translate partial-result warnings without presenting them as fatal errors."""
    raw = str(message or "未知提醒").strip()
    lowered = raw.lower()
    label = _PLATFORM_LABELS.get(platform, platform or "当前平台")
    if "creator timeline unavailable" in lowered:
        return f"{label}账号作品列表暂时无法完整读取，已保留其他方式找到的内容。", "稍后重试；频繁出现时更新该平台 Cookie 或重新登录。"
    if "fallback search scanned" in lowered:
        return f"{label}主页接口受限，备用搜索已翻页并保留精确匹配该账号的作品。", "若仍未达到目标，请更新该平台 Cookie 后重试；备用搜索不保证覆盖账号全部历史作品。"
    if "pagination stopped" in lowered or "cursor made no progress" in lowered:
        return f"{label}分页在后续页面中断，已保留中断前取得的内容。", "稍后从断点继续；频繁出现时更新登录状态或降低单次作品数。"
    if "video url unavailable" in lowered:
        return "部分作品没有取得可直接下载的视频地址。", "确认作品可以公开播放后重试，或减少单次检索作品数。"
    if "missing or mismatched author" in lowered:
        return "部分作品的作者信息与目标账号不一致，已为避免下载错账号而跳过。", "优先使用完整主页链接；用昵称时请确认没有同名账号。"
    if "missing publication date" in lowered:
        return "部分作品没有发布时间，无法按发布时间完整排序。", "下载结果仍可使用；需要严格时间顺序时请稍后重试。"
    if "filter" in lowered or "筛选" in raw:
        return "部分内容无法完成筛选，已按当前筛选模式处理可用结果。", "需要严格筛选时检查本地模型后重试；否则可使用“尽量筛选”。"
    if "hls" in lowered or "dash" in lowered:
        return "页面中的部分视频是分段流，当前无法作为单个视频文件直接下载。", "改用该视频的作品链接，或选择平台专用采集链路。"
    if "动态页面" in raw or "详情页" in raw or "time" in lowered:
        return "部分动态页面或详情页没有读取完成，已保留成功找到的内容。", "减少“最多检索作品数”后重试，并检查网络连接。"
    return f"{label}返回了部分结果：{raw}", "查看实际数量；如未达到目标，可稍后重试或减少单次任务数量。"


def _task_report(body: dict, result: dict, elapsed_seconds: float) -> dict:
    """Build a stable, user-facing account of requested and actual results."""
    platform = str((body.get("platforms") or [""])[0] or "")
    media_type = str(body.get("media_type") or "images")
    requested = {
        "images": int(body.get("image_limit") or 0) if media_type != "videos" else 0,
        "videos": int(body.get("video_limit") or 0) if media_type != "images" else 0,
        "max_posts": int(body.get("max_posts") or 0),
        "per_post_limit": int(body.get("per_post_limit") or 0) if media_type != "videos" else 0,
    }
    items = [item for item in result.get("items", []) if isinstance(item, dict)]
    downloads = [item for item in result.get("downloads", []) if isinstance(item, dict)]
    by_id = {str(item.get("id") or ""): str(item.get("media_type") or "image") for item in items}
    found = {"images": 0, "videos": 0}
    for item in items:
        found["videos" if item.get("media_type") == "video" else "images"] += 1
    status_counts = {
        name: {"images": 0, "videos": 0}
        for name in ("downloaded", "existing", "duplicate", "rejected", "failed")
    }
    failure_messages: list[str] = []
    for record in downloads:
        status_name = str(record.get("status") or "failed")
        if status_name not in status_counts:
            status_name = "failed"
        media = str(record.get("media_type") or by_id.get(str(record.get("candidate_id") or ""), "image"))
        bucket = "videos" if media == "video" else "images"
        status_counts[status_name][bucket] += 1
        if status_name in {"failed", "rejected"} and record.get("error"):
            failure_messages.append(str(record["error"]))

    logs: list[dict[str, str]] = []
    target_parts = []
    if requested["images"]:
        target_parts.append(f"{requested['images']} 张图片")
    if requested["videos"]:
        target_parts.append(f"{requested['videos']} 个视频")
    scope = f"，最多检查 {requested['max_posts']} 个作品" if requested["max_posts"] else ""
    logs.append({"level": "info", "message": f"目标：{'，'.join(target_parts) or '检索媒体'}{scope}。"})
    if requested["images"] > 200 or requested["videos"] > 200 or requested["max_posts"] > 100:
        logs.append({
            "level": "warning",
            "message": "这是大批量任务，程序会分页采集并继续使用增量去重。",
            "action": "请保持桌面版运行；CLI 仍受平台访问频率和登录状态限制，遇到验证时请按后续日志处理。",
        })

    identity = result.get("identity") if isinstance(result.get("identity"), dict) else None
    if identity:
        account = str(identity.get("name") or identity.get("canonical_id") or "已确认账号")
        logs.append({"level": "success", "message": f"已确认账号：{account}。"})

    actual_parts = []
    if requested["images"]:
        actual_parts.append(f"找到 {found['images']} 张图片")
    if requested["videos"]:
        actual_parts.append(f"找到 {found['videos']} 个视频")
    logs.append({"level": "info", "message": f"采集结果：{'，'.join(actual_parts) or '没有找到媒体'}。"})
    pages_fetched = int(result.get("current_pages_fetched", result.get("pages_fetched")) or 0)
    posts_fetched = int(result.get("current_posts_fetched", result.get("posts_fetched")) or 0)
    if pages_fetched or posts_fetched:
        logs.append({
            "level": "info",
            "message": f"本次检索：读取 {pages_fetched} 页，检查 {posts_fetched} 个作品。",
        })
    stop_messages = {
        "target_reached": "候选内容已达到目标，程序主动停止翻页。",
        "post_limit_reached": "已达到本次最多检索作品数，分页结束。",
        "source_exhausted": "平台未返回下一页游标，已自动翻到当前可读取的末页。",
        "cursor_stalled": "平台重复返回同一页游标，为避免死循环已停止翻页。",
        "pagination_error": "后续分页读取失败，已保留中断前取得的内容。",
    }
    stop_reason = result.get("current_pagination_stop_reason")
    if stop_reason in stop_messages:
        logs.append({
            "level": "warning" if stop_reason in {"cursor_stalled", "pagination_error"} else "info",
            "message": stop_messages[stop_reason],
        })

    new_total = sum(status_counts["downloaded"].values())
    existing_total = sum(status_counts["existing"].values())
    duplicate_total = sum(status_counts["duplicate"].values())
    rejected_total = sum(status_counts["rejected"].values())
    failed_total = sum(status_counts["failed"].values())
    if downloads:
        logs.append({
            "level": "success" if not failed_total and not rejected_total else "warning",
            "message": (
                f"保存结果：新下载 {new_total} 个，已存在 {existing_total} 个，"
                f"重复跳过 {duplicate_total} 个，规则拒绝 {rejected_total} 个，失败 {failed_total} 个。"
            ),
        })

    issues: list[dict[str, str]] = []
    raw_errors: list[str] = []
    top_error = result.get("error")
    if isinstance(top_error, dict) and top_error.get("message"):
        raw_errors.append(str(top_error["message"]))
    elif isinstance(top_error, str) and top_error:
        raw_errors.append(top_error)
    platform_results = result.get("platforms") if isinstance(result.get("platforms"), dict) else {}
    for entry in platform_results.values():
        error = entry.get("error") if isinstance(entry, dict) else None
        if isinstance(error, dict) and error.get("message"):
            raw_errors.append(str(error["message"]))
        elif isinstance(error, str) and error:
            raw_errors.append(error)
    raw_errors.extend(failure_messages)
    for raw_error in dict.fromkeys(raw_errors):
        reason, action = _error_guidance(raw_error, platform)
        issues.append({"reason": reason, "action": action, "technical_message": raw_error})

    raw_warnings = [str(value) for value in result.get("warnings", []) if value]
    for key in ("filter_warning", "object_detection_error"):
        if result.get(key):
            raw_warnings.append(str(result[key]))
    for raw_warning in dict.fromkeys(raw_warnings):
        reason, action = _warning_guidance(raw_warning, platform)
        issues.append({"reason": reason, "action": action, "technical_message": raw_warning})

    filtered = int(result.get("rejected_by_content_filter") or 0)
    if filtered:
        issues.append({
            "reason": f"内容筛选排除了 {filtered} 个候选。",
            "action": "放宽内容描述或筛选模式后重试。",
            "technical_message": "content filter rejected candidates",
        })

    successful = {
        media: status_counts["downloaded"][media] + status_counts["existing"][media]
        for media in ("images", "videos")
    }
    shortfalls = {
        media: max(0, requested[media] - (successful[media] if body.get("download", True) else found[media]))
        for media in ("images", "videos")
    }
    for media, unit in (("images", "张图片"), ("videos", "个视频")):
        if not shortfalls[media] or raw_errors:
            continue
        reason = f"实际可用数量比目标少 {shortfalls[media]} {unit}。"
        if requested["max_posts"] and posts_fetched >= requested["max_posts"]:
            action = "已检查完本次设置的作品范围；可增加“最多检索作品数”，或更新平台登录状态后重试。"
        else:
            action = "程序已自动读取平台返回的后续页面；若仍不足，请更新平台登录状态后重试。"
        if media == "images" and requested["per_post_limit"] and requested["max_posts"]:
            capacity = requested["per_post_limit"] * requested["max_posts"]
            if requested["images"] > capacity:
                reason = f"当前设置理论最多只能取得 {capacity} 张图片，低于目标 {requested['images']} 张。"
                action = "提高“最多检索作品数”或“每个作品最多张数”。"
        if not items and result.get("refresh_count") and result.get("post_ids"):
            reason = "没有新增文件；该账号已发现的作品之前已经处理过。"
            action = "这是增量采集的正常结果，等待账号发布新作品后再次运行。"
        issues.append({"reason": reason, "action": action, "technical_message": "requested quantity was not fulfilled"})

    error_messages = set(raw_errors)
    for issue in issues:
        level = "error" if issue["technical_message"] in error_messages else "warning"
        logs.append({"level": level, "message": issue["reason"], "action": issue["action"]})
    if result.get("output_dir"):
        logs.append({"level": "info", "message": f"保存目录：{result['output_dir']}"})
    logs.append({"level": "info", "message": f"任务耗时：{elapsed_seconds:.1f} 秒。"})

    requested_total = requested["images"] + requested["videos"]
    available_total = successful["images"] + successful["videos"] if body.get("download", True) else found["images"] + found["videos"]
    report_status = "failed" if raw_errors and available_total == 0 else ("partial" if issues or available_total < requested_total else "success")
    return {
        "status": report_status,
        "requested": requested,
        "found": found,
        "downloads": status_counts,
        "successful": successful,
        "shortfalls": shortfalls,
        "issues": issues,
        "logs": logs,
    }


def _request_timeout(body: dict) -> float:
    """Give large media jobs enough wall time without making small jobs unbounded."""
    media_type = str(body.get("media_type") or "images")
    images = int(body.get("image_limit") or 0) if media_type != "videos" else 0
    videos = int(body.get("video_limit") or 0) if media_type != "images" else 0
    posts = int(body.get("max_posts") or 20)
    return min(7200.0, max(300.0, 120.0 + posts * 1.5 + images * 3.0 + videos * 20.0))


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


async def _search_with_progress(search_kwargs: dict, creator_name: str | None, creator_id: str | None, callback):
    with progress_context(callback):
        return await search_images(**search_kwargs, creator_name=creator_name, creator_id=creator_id)


def _execute_search(body: dict, runner: _Loop, callback) -> dict:
    query = str(body["query"])
    platforms = body.get("platforms") or None
    detected = parse_intent(query).identifier_platform
    # A pasted platform URL is authoritative. This prevents a selected radio
    # button from routing a URL to the wrong adapter.
    if detected and platforms and detected not in platforms:
        platforms = None
    selected_platform = platforms[0] if platforms and len(platforms) == 1 else None
    auto_creator, auto_creator_id = _creator_hint(query, selected_platform, detected)

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
    request_timeout = _request_timeout(body)
    result = runner.call(
        _search_with_progress(search_kwargs, auto_creator, auto_creator_id, callback),
        timeout=request_timeout,
    )
    error = result.get("error") if isinstance(result, dict) else None
    if (auto_creator or auto_creator_id) and isinstance(error, dict):
        message = str(error.get("message", "")).lower()
        if any(marker in message for marker in (
            "matched 0", "not found", "invalid creator",
            "creator_identity_unresolved", "identity_unresolved",
        )):
            callback({
                "stage": "retrieving", "level": "warning",
                "message": "未确认到唯一账号，正在改用关键词检索。",
            })
            result = runner.call(
                _search_with_progress(search_kwargs, None, None, callback),
                timeout=request_timeout,
            )
    if not isinstance(result, dict):
        raise ValueError("采集服务返回了无法识别的结果")
    return result


def _run_search_task(task_id: str, body: dict, runner: _Loop, tasks: TaskRegistry) -> None:
    started = time.monotonic()
    tasks.start(task_id)
    callback = lambda event: tasks.update(task_id, event)
    try:
        result = _execute_search(body, runner, callback)
        report = _task_report(body, result, time.monotonic() - started)
        result["task_report"] = report
        tasks.update(task_id, {
            "pages_fetched": result.get("current_pages_fetched", result.get("pages_fetched", 0)),
            "posts_fetched": result.get("current_posts_fetched", result.get("posts_fetched", 0)),
            "images_found": report["found"]["images"],
            "videos_found": report["found"]["videos"],
            **{
                name: sum(report["downloads"][name].values())
                for name in ("downloaded", "existing", "duplicate", "rejected", "failed")
            },
        })
        tasks.finish(task_id, result, failed=report["status"] == "failed")
    except Exception as exc:
        error_result = {
            "items": [], "downloads": [],
            "error": {"code": "request_error", "message": str(exc)},
        }
        error_result["task_report"] = _task_report(body, error_result, time.monotonic() - started)
        tasks.finish(task_id, error_result, failed=True)


class Handler(BaseHTTPRequestHandler):
    runner: _Loop
    tasks: TaskRegistry = TASKS

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
        elif parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.removeprefix("/api/tasks/").strip("/")
            task = self.tasks.get(task_id)
            self._send(200, task) if task else self._send(404, {"error": "task not found"})
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
            task = self.tasks.create()
            worker = threading.Thread(
                target=_run_search_task,
                args=(task["task_id"], body, self.runner, self.tasks),
                daemon=True,
            )
            worker.start()
            self._send(202, task)
        except Exception as exc:
            self._send(400, {"error": {"code": "request_error", "message": str(exc)}})


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
