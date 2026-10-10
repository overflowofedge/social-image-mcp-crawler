from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .intent import Intent, expand_token
from .models import CreatorFetchRequest, CreatorIdentity, ImageCandidate, Platform, SearchRequest


class SourceError(RuntimeError):
    pass


class SourceUnavailable(SourceError):
    pass


def _decode_process_output(data: bytes) -> str:
    """Decode UTF-8 first, then Windows Simplified Chinese output."""
    if not data:
        return ""
    for encoding in ("utf-8", "gb18030"):
        text = data.decode(encoding, errors="replace")
        if "\ufffd" not in text:
            return text
    return data.decode("utf-8", errors="replace")


class SourceVerificationStore:
    """Persist real source outcomes without storing credentials or content."""

    def __init__(self, path: str | Path | None = None, ttl_seconds: int = 86400) -> None:
        self.path = Path(path).expanduser() if path else None
        self.ttl_seconds = max(1, ttl_seconds)
        self._lock = threading.RLock()
        self._state: dict[str, dict[str, dict[str, Any]]] = self._load()

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self.path is None:
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if self.path is None:
            return
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            # Verification is evidence, not part of the search result. A
            # read-only or briefly locked cache must never turn a successful
            # platform request into an MCP error.
            with contextlib.suppress(OSError):
                temporary.unlink()

    @staticmethod
    def _iso(timestamp: float | None) -> str | None:
        if not timestamp:
            return None
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")

    def record_success(self, source: str, platform: str) -> None:
        with self._lock:
            record = self._state.setdefault(source, {}).setdefault(platform, {})
            record.update({"last_success": time.time(), "last_result": "success", "last_error": None})
            self._save()

    def record_failure(self, source: str, platform: str, detail: str) -> None:
        with self._lock:
            record = self._state.setdefault(source, {}).setdefault(platform, {})
            record.update({"last_failure": time.time(), "last_result": "failure", "last_error": detail[-1000:]})
            self._save()

    def status(self, source: str, platform: str) -> dict[str, Any]:
        with self._lock:
            record = dict(self._state.get(source, {}).get(platform, {}))
        last_success = float(record.get("last_success") or 0)
        verified = bool(last_success and time.time() - last_success <= self.ttl_seconds)
        last_result = record.get("last_result")
        return {
            "verified": verified,
            "ready": verified and last_result == "success",
            "last_result": last_result,
            "last_verified_at": self._iso(last_success),
            "last_failure_at": self._iso(float(record.get("last_failure") or 0)),
            "last_error": record.get("last_error"),
        }


def _latest_verification_event(platform_status: dict[str, dict[str, Any]]) -> dict[str, str] | None:
    """Return the latest persisted outcome across a source's platforms."""
    events: list[dict[str, str]] = []
    for status in platform_status.values():
        result = status.get("last_result")
        if result == "failure" and status.get("last_failure_at"):
            events.append({
                "at": str(status["last_failure_at"]),
                "result": "failure",
                "error": str(status.get("last_error") or "unknown source failure"),
            })
        elif result == "success" and status.get("last_verified_at"):
            events.append({
                "at": str(status["last_verified_at"]),
                "result": "success",
                "error": "",
            })
    return max(events, key=lambda event: event["at"]) if events else None


def _process_spawn_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


async def _terminate_process_tree(process: asyncio.subprocess.Process | None) -> None:
    """Terminate a source and descendants (notably Playwright browsers)."""
    if process is None or process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(killer.wait(), timeout=5)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)


@dataclass(frozen=True)
class SourceStatus:
    name: str
    configured: bool
    mode: str
    detail: str
    platforms: tuple[str, ...]
    verified: bool = False
    verified_platforms: tuple[str, ...] = ()
    ready_platforms: tuple[str, ...] = ()
    platform_status: dict[str, dict[str, Any]] | None = None


@dataclass(frozen=True)
class CreatorSourceResult:
    identity: CreatorIdentity
    items: list[ImageCandidate]
    posts_fetched: int
    next_cursor: str | None = None
    post_ids: tuple[str, ...] = ()
    rejected_posts: int = 0
    pages_fetched: int = 0
    warnings: tuple[str, ...] = ()


def _json_values(text: str) -> list[Any]:
    text = text.strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    values: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def _records(value: Any) -> list[Any]:
    if isinstance(value, dict):
        for key in ("data", "items", "results", "notes", "posts", "media", "list"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    if isinstance(value, list):
        # gallery-dl emits [type, payload] or [type, url, metadata].
        if value and isinstance(value[0], int) and len(value) >= 2:
            return [value]
        return value
    return []


def _embedded_error(value: Any) -> str | None:
    """Extract structured CLI errors that are encoded as JSON records."""
    if isinstance(value, dict):
        error = value.get("error")
        if error:
            message = value.get("message") or value.get("description") or ""
            return f"{error}: {message}".rstrip(": ")
        for child in value.values():
            if found := _embedded_error(child):
                return found
    elif isinstance(value, list):
        for child in value:
            if found := _embedded_error(child):
                return found
    return None


def _urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("image_url", "imageUrl", "url", "murl", "display_url", "displayUrl", "original_url", "originalUrl", "media_url", "mediaUrl", "thumbnail_url", "thumbnailUrl"):
            result.extend(_urls(value.get(key)))
        for key in ("images", "image_urls", "imageUrls", "media", "resources", "files"):
            result.extend(_urls(value.get(key)))
        return list(dict.fromkeys(result))
    if isinstance(value, list):
        return list(dict.fromkeys(url for child in value for url in _urls(child)))
    return []


def _first(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        item = value.get(key)
        if item not in (None, "", []):
            return item
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _media_type(metadata: dict[str, Any], image_url: str) -> str:
    """Infer whether a gallery-dl record is an image or downloadable video."""
    if metadata.get("media_type") in {"image", "video"}:
        return metadata["media_type"]
    values = [
        metadata.get("media_type"), metadata.get("mimetype"), metadata.get("mime_type"),
        metadata.get("content_type"), metadata.get("extension"), metadata.get("ext"),
        metadata.get("filename"), metadata.get("video_url"), metadata.get("videoUrl"), image_url,
    ]
    text = " ".join(str(value or "").lower() for value in values)
    return "video" if any(marker in text for marker in ("video/", ".mp4", ".webm", ".mkv", " mp4", " webm")) else "image"


def _candidate(platform: Platform, record: Any, image_url: str, index: int, source_name: str) -> ImageCandidate:
    metadata: dict[str, Any]
    if isinstance(record, list) and record and isinstance(record[0], int):
        metadata = record[2] if len(record) > 2 and isinstance(record[2], dict) else (record[1] if isinstance(record[1], dict) else {})
    elif isinstance(record, dict):
        metadata = record
    else:
        metadata = {}
    item_id = str(_first(metadata, "id", "item_id", "itemId", "note_id", "noteId", "tweet_id", "tweetId", "post_id", "postId") or index)
    post_id = str(_first(metadata, "post_id", "postId", "aweme_id", "note_id", "mid") or item_id.split(":", 1)[0])
    title = str(_first(metadata, "title", "text", "content", "caption", "description", "desc", "alt_description") or "")
    author = str(_first(metadata, "author", "author_name", "username", "user_name", "user") or "")
    return ImageCandidate(
        id=item_id,
        platform=platform,
        image_url=image_url,
        media_type=_media_type(metadata, image_url),
        thumbnail_url=_first(metadata, "thumbnail_url", "thumbnailUrl", "thumb", "preview") if isinstance(_first(metadata, "thumbnail_url", "thumbnailUrl", "thumb", "preview"), str) else None,
        permalink=_first(metadata, "permalink", "post_url", "postUrl", "web_url", "webUrl", "page_url", "pageUrl", "url") if isinstance(_first(metadata, "permalink", "post_url", "postUrl", "web_url", "webUrl", "page_url", "pageUrl", "url"), str) else None,
        title=title,
        description=title,
        author=author,
        alt_text=str(_first(metadata, "alt_text", "alt", "accessibility_alt_text") or ""),
        width=_first(metadata, "width", "image_width"),
        height=_first(metadata, "height", "image_height"),
        published_at=_first(metadata, "published_at", "created_at", "timestamp"),
        creator_id=str(_first(metadata, "creator_id", "creatorId", "author_id", "user_id") or "") or None,
        creator_name=str(_first(metadata, "creator_name", "creatorName", "author", "username") or author),
        post_id=post_id,
        media_index=max(1, int(_number(_first(metadata, "media_index", "image_index", "imageIndex", "num"), 1))),
        engagement_score=max(0.0, _number(_first(metadata, "engagement_score", "engagementScore"), 0)),
        source_payload={"source": source_name, "record": metadata},
    )


def normalize_source_output(platform: Platform, output: str, source_name: str, limit: int, media_type: str | None = None) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    counts = {"image": 0, "video": 0}
    for value in _json_values(output):
        for index, record in enumerate(_records(value)):
            # gallery-dl's [type, media_url, metadata] uses metadata.url for
            # the source page. Types 2 and 6 are directory/queue messages,
            # not downloadable images; only type 3 is actual media.
            if isinstance(record, list) and record and isinstance(record[0], int):
                if record[0] != 3 or len(record) <= 1:
                    continue
                image_urls = _urls(record[1])
                if not image_urls and len(record) > 2 and isinstance(record[2], dict):
                    # Instagram emits a ``ytdl:`` pseudo URL for videos and
                    # keeps the signed CDN URL in metadata.video_url.
                    image_urls = _urls(record[2].get("video_url") or record[2].get("videoUrl"))
            elif isinstance(record, dict) and record.get("image_url"):
                # Normalized records carry a single media URL. The permalink
                # and video thumbnail are metadata, not additional downloads.
                image_urls = _urls(record["image_url"])
            else:
                image_urls = _urls(record)
            for image_url in image_urls:
                item = _candidate(platform, record, image_url, len(candidates) + index, source_name)
                if media_type in {"images", "videos"} and item.media_type != media_type[:-1]:
                    continue
                if media_type == "all" and counts[item.media_type] >= limit:
                    continue
                candidates.append(item)
                counts[item.media_type] += 1
                if media_type != "all" and len(candidates) >= limit:
                    return candidates
    return candidates


def normalize_creator_source_output(
    platform: Platform,
    output: str,
    source_name: str,
    limit: int,
) -> CreatorSourceResult:
    envelopes = [value for value in _json_values(output) if isinstance(value, dict) and isinstance(value.get("identity"), dict)]
    if not envelopes:
        raise SourceError(f"{source_name} returned no creator identity envelope")
    envelope = envelopes[-1]
    identity_payload = {**envelope["identity"], "platform": platform.value, "source": source_name}
    identity = CreatorIdentity.model_validate(identity_payload)
    candidates = normalize_source_output(platform, json.dumps({"items": envelope.get("items") or []}, ensure_ascii=False), source_name, limit)
    return CreatorSourceResult(
        identity=identity,
        items=candidates,
        posts_fetched=max(0, int(envelope.get("posts_fetched") or 0)),
        next_cursor=str(envelope["next_cursor"]) if envelope.get("next_cursor") not in (None, "", "0", 0) else None,
        post_ids=tuple(str(value) for value in envelope.get("post_ids", [])),
        rejected_posts=int(envelope.get("rejected_posts") or 0),
        pages_fetched=int(envelope.get("pages_fetched") or 0),
        warnings=tuple(str(value) for value in envelope.get("warnings", [])),
    )


class ExternalJsonSource:
    """Runs a configured source project through a small stdout JSON contract."""

    def __init__(self, name: str, command_template: str | None, platforms: tuple[Platform, ...], timeout_seconds: int = 120, failure_cooldown_seconds: int = 120, verification_store: SourceVerificationStore | None = None) -> None:
        self.name = name
        self.command_template = command_template
        self.platforms = platforms
        self.timeout_seconds = timeout_seconds
        self.failure_cooldown_seconds = max(0, failure_cooldown_seconds)
        self._verified = False
        self._last_error: str | None = None
        self._verified_platforms: set[str] = set()
        self._last_errors: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._verification = verification_store or SourceVerificationStore()

    @property
    def status(self) -> SourceStatus:
        configured = bool(self.command_template)
        platform_status = {platform.value: self._verification.status(self.name, platform.value) for platform in self.platforms}
        verified_platforms = tuple(sorted(platform for platform, status in platform_status.items() if status["verified"]))
        ready_platforms = tuple(sorted(platform for platform, status in platform_status.items() if status["ready"]))
        latest_event = _latest_verification_event(platform_status)
        if not configured:
            detail = "Set the source command environment variable"
        elif self._last_error:
            detail = f"Last request failed: {self._last_error}"
        elif latest_event and latest_event["result"] == "failure":
            detail = f"Last request failed: {latest_event['error']}"
        elif verified_platforms:
            detail = "Last request completed successfully and returned a valid source response"
        else:
            detail = "Configured command is not verified until a real request completes"
        return SourceStatus(
            self.name,
            configured,
            "external-command" if configured else "not-configured",
            detail,
            tuple(platform.value for platform in self.platforms),
            bool(verified_platforms),
            verified_platforms,
            ready_platforms,
            platform_status,
        )

    def _failed(self, platform: Platform, detail: str, scope: str | None = None) -> None:
        cooldown_key = f"{platform.value}:{scope}" if scope else platform.value
        self._verified_platforms.discard(platform.value)
        self._last_errors[platform.value] = detail
        self._verified = bool(self._verified_platforms)
        self._last_error = detail
        if self.failure_cooldown_seconds:
            self._cooldown_until[cooldown_key] = time.monotonic() + self.failure_cooldown_seconds
        self._verification.record_failure(self.name, platform.value, detail)

    def _succeeded(self, platform: Platform, scope: str | None = None) -> None:
        self._verified_platforms.add(platform.value)
        self._last_errors.pop(platform.value, None)
        self._verified = True
        self._last_error = None
        self._cooldown_until.pop(platform.value, None)
        if scope:
            self._cooldown_until.pop(f"{platform.value}:{scope}", None)
        self._verification.record_success(self.name, platform.value)

    def _check_cooldown(self, platform: Platform, scope: str | None = None) -> None:
        cooldown_key = f"{platform.value}:{scope}" if scope else platform.value
        until = self._cooldown_until.get(cooldown_key, 0.0)
        if until <= time.monotonic():
            self._cooldown_until.pop(cooldown_key, None)
            return
        remaining = max(1, int(until - time.monotonic()))
        detail = self._last_errors.get(platform.value, "previous request failed")
        target = f"{platform.value}/{scope}" if scope else platform.value
        raise SourceError(
            f"{self.name} temporarily skipped for {target} for {remaining}s "
            f"after a previous failure: {detail}"
        )

    def _command(self, platform: Platform, intent: Intent, limit: int) -> list[str]:
        if not self.command_template:
            raise SourceUnavailable(self.status.detail)
        values = {"platform": platform.value, "query": intent.raw, "item_id": intent.identifier or "", "url": intent.url or "", "limit": str(limit)}
        rendered = self.command_template.format(**values)
        return shlex.split(rendered, posix=True)

    async def fetch_creator(self, request: CreatorFetchRequest) -> CreatorSourceResult:
        if request.platform not in self.platforms:
            raise SourceUnavailable(f"{self.name} does not support creator retrieval for {request.platform.value}")
        scope = request.profile_url or request.creator_id or request.creator_name or "creator"
        self._check_cooldown(request.platform, scope)
        # The structured request is passed out of band, never interpolated into
        # a shell/template. Existing bridge configuration remains compatible.
        command = self._command(request.platform, Intent("", "", (), ()), request.max_images)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "SOCIAL_IMAGE_CREATOR_REQUEST": request.model_dump_json()}
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, **_process_spawn_options(),
            )
            media_target = request.max_images + (request.max_videos or 0)
            task_timeout = min(
                1800.0,
                max(float(self.timeout_seconds), 60.0 + request.max_posts * 2.0 + media_target * 0.5),
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=task_timeout)
            if process.returncode:
                detail = f"{self.name} creator request failed: {_decode_process_output(stderr)[-1500:]}"
                raise SourceError(detail)
            result = normalize_creator_source_output(request.platform, _decode_process_output(stdout), self.name, 20000)
            if result.identity.platform != request.platform:
                detail = "creator identity platform mismatch"
                raise SourceError(detail)
            self._succeeded(request.platform, scope)
            return result
        except asyncio.TimeoutError as exc:
            await _terminate_process_tree(process)
            detail = f"{self.name} creator request timed out after {task_timeout:.0f}s"
            self._failed(request.platform, detail, scope)
            raise SourceError(detail) from exc
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            raise
        except SourceError as exc:
            # This also covers malformed JSON/envelopes from a source process;
            # Record it so this creator-specific cooldown prevents a retry loop.
            self._failed(request.platform, str(exc), scope)
            raise
        except OSError as exc:
            detail = f"{self.name} creator process failed: {exc}"
            self._failed(request.platform, detail, scope)
            raise SourceError(detail) from exc
        except Exception as exc:
            detail = f"{self.name} creator response invalid: {exc}"
            self._failed(request.platform, detail, scope)
            raise SourceError(detail) from exc

    async def search(self, platform: Platform, intent: Intent, limit: int, *, request: SearchRequest | None = None) -> list[ImageCandidate]:
        if platform not in self.platforms:
            raise SourceUnavailable(f"{self.name} does not support {platform.value}")
        self._check_cooldown(platform)
        command = self._command(platform, intent, limit)
        env = os.environ.copy()
        env.update({
            "SOCIAL_IMAGE_PLATFORM": platform.value,
            "SOCIAL_IMAGE_QUERY": intent.raw,
            "SOCIAL_IMAGE_ITEM_ID": intent.identifier or "",
            "SOCIAL_IMAGE_LIMIT": str(limit),
            "SOCIAL_IMAGE_SEARCH_MAX_POSTS": str(request.max_posts if request else min(limit, 500)),
            # Windows defaults to the active code page (often GBK).  Source
            # bridges emit JSON metadata that may contain emoji or other
            # Unicode, so force UTF-8 for both Python and compatible CLIs.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        if request is not None:
            env["SOCIAL_IMAGE_SEARCH_REQUEST"] = request.model_dump_json()
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                **_process_spawn_options(),
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._failed(platform, f"timed out after {self.timeout_seconds}s")
            if process is not None:
                await _terminate_process_tree(process)
            raise SourceError(
                f"{self.name} timed out after {self.timeout_seconds}s; "
                "the source process did not finish (login, platform verification, or process startup may be blocked)"
            ) from exc
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process_tree(process)
            raise
        except OSError as exc:
            self._failed(platform, str(exc))
            raise SourceError(f"{self.name} process failed to start: {exc}") from exc
        if process.returncode != 0:
            detail = _decode_process_output(stderr).strip()[-1000:]
            self._failed(platform, detail or f"exited with code {process.returncode}")
            raise SourceError(f"{self.name} exited with code {process.returncode}: {detail}")
        candidates = normalize_source_output(platform, _decode_process_output(stdout), self.name, limit, request.media_type if request else None)
        if not candidates:
            values = _json_values(_decode_process_output(stdout))
            if any(isinstance(value, dict) and value.get("source_status") == "empty" for value in values):
                # A valid empty page is content state, not source failure. It
                # must not put the whole platform into a cooldown.
                return []
            media_label = {"images": "image", "videos": "video"}.get(request.media_type if request else "images", "media")
            self._failed(platform, f"no {media_label} candidates returned for {platform.value}")
            raise SourceError(
                f"{self.name} returned no {media_label} candidates for {platform.value}; "
                "check login state, query, and source-project output"
            )
        self._succeeded(platform)
        return candidates


class GalleryDlSource:
    """gallery-dl bridge for X/Instagram/Weibo URLs and search pages."""

    def __init__(self, binary: str = "gallery-dl", config_path: str | None = None, timeout_seconds: int = 120, cookies_from_browser: str | None = None, failure_cooldown_seconds: int = 120, verification_store: SourceVerificationStore | None = None, cookies_file: str | None = None, *, platform_cookies_from_browser: dict[Platform, str | None] | None = None, platform_cookie_files: dict[Platform, str | None] | None = None, name: str = "gallery-dl", platforms: tuple[Platform, ...] | None = None) -> None:
        self.name = name
        self.binary = binary
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds
        self.cookies_from_browser = cookies_from_browser
        self.cookies_file = str(Path(cookies_file).expanduser()) if cookies_file else None
        self.platform_cookies_from_browser = {
            platform: value for platform, value in (platform_cookies_from_browser or {}).items() if value
        }
        self.platform_cookie_files = {
            platform: str(Path(value).expanduser())
            for platform, value in (platform_cookie_files or {}).items() if value
        }
        self.failure_cooldown_seconds = max(0, failure_cooldown_seconds)
        self.platforms = platforms or (Platform.X, Platform.INSTAGRAM, Platform.WEIBO, Platform.XHS)
        self._verified = False
        self._last_error: str | None = None
        self._verified_platforms: set[str] = set()
        self._last_errors: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._verification = verification_store or SourceVerificationStore()

    @property
    def status(self) -> SourceStatus:
        found = Path(self.binary).exists() or shutil.which(self.binary) is not None
        platform_status = {platform.value: self._verification.status(self.name, platform.value) for platform in self.platforms}
        verified_platforms = tuple(sorted(platform for platform, status in platform_status.items() if status["verified"]))
        ready_platforms = tuple(sorted(platform for platform, status in platform_status.items() if status["ready"]))
        latest_event = _latest_verification_event(platform_status)
        if not found:
            detail = "Install gallery-dl or set GALLERY_DL_BINARY"
        elif self._last_error:
            detail = f"Last request failed: {self._last_error}"
        elif latest_event and latest_event["result"] == "failure":
            detail = f"Last request failed: {latest_event['error']}"
        elif verified_platforms:
            detail = "Last request completed successfully and returned media"
        else:
            detail = "gallery-dl is installed; account access is not verified until a real request"
        return SourceStatus(
            self.name,
            found,
            "native-cli" if found else "not-installed",
            detail,
            tuple(platform.value for platform in self.platforms),
            bool(verified_platforms),
            verified_platforms,
            ready_platforms,
            platform_status,
        )

    def _failed(self, platform: Platform, detail: str) -> None:
        self._verified_platforms.discard(platform.value)
        self._last_errors[platform.value] = detail
        self._verified = bool(self._verified_platforms)
        self._last_error = detail
        if self.failure_cooldown_seconds:
            self._cooldown_until[platform.value] = time.monotonic() + self.failure_cooldown_seconds
        self._verification.record_failure(self.name, platform.value, detail)

    def _succeeded(self, platform: Platform) -> None:
        self._verified_platforms.add(platform.value)
        self._last_errors.pop(platform.value, None)
        self._verified = True
        self._last_error = None
        self._cooldown_until.pop(platform.value, None)
        self._verification.record_success(self.name, platform.value)

    def _check_cooldown(self, platform: Platform) -> None:
        until = self._cooldown_until.get(platform.value, 0.0)
        if until <= time.monotonic():
            self._cooldown_until.pop(platform.value, None)
            return
        remaining = max(1, int(until - time.monotonic()))
        detail = self._last_errors.get(platform.value, "previous request failed")
        raise SourceError(
            f"{self.name} temporarily skipped for {platform.value} for {remaining}s "
            f"after a previous failure: {detail}"
        )

    @staticmethod
    def _instagram_tags(intent: Intent) -> list[str]:
        controls = {
            "横图", "竖图", "方图", "高清", "高分辨率", "原图", "无水印",
            "不要水印", "去水印", "无文字", "不要文字", "无字", "landscape",
            "horizontal", "wide", "portrait", "vertical", "tall", "square",
            "hd", "4k", "high", "resolution",
        }
        ordered = sorted(
            (token for token in intent.tokens if token not in controls),
            key=lambda token: (intent.normalized.find(token), -len(token)),
        )
        tags: list[str] = []
        for token in ordered:
            aliases = [value for value in expand_token(token) if value.isascii()]
            if aliases:
                # Hashtags cannot contain spaces. Prefer the most descriptive
                # known alias ("coffee shop" -> "coffeeshop").
                value = max(aliases, key=len)
            elif any(
                token != other
                and other in token
                and any(value.isascii() for value in expand_token(other))
                for other in ordered
            ):
                # The tokenizer also keeps the full Chinese phrase. Skip it
                # when translated subphrases already provide useful tags.
                continue
            else:
                value = token
            compact = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]", "", value)
            if compact and compact.lower() not in {part.lower() for part in tags}:
                tags.append(compact)
            if len(tags) >= 3:
                break
        return tags or [re.sub(r"\W", "", intent.raw)[:80]]

    def _targets(self, platform: Platform, intent: Intent) -> list[str]:
        if intent.url:
            return [intent.url]
        if intent.identifier:
            return [{
                Platform.X: f"https://x.com/i/status/{intent.identifier}",
                Platform.INSTAGRAM: f"https://www.instagram.com/p/{intent.identifier}/",
                Platform.WEIBO: f"https://weibo.com/detail/{intent.identifier}",
            }[platform]]
        if platform == Platform.X:
            query = intent.raw
            if "filter:media" not in query.lower() and "filter:images" not in query.lower():
                query += " filter:media"
            return [f"https://x.com/search?q={quote(query)}&src=typed_query"]
        if platform == Platform.INSTAGRAM:
            return [f"https://www.instagram.com/explore/tags/{quote(tag)}/" for tag in self._instagram_tags(intent)]
        if platform == Platform.XHS:
            return [intent.url] if intent.url else [f"https://www.xiaohongshu.com/search_result?keyword={quote(intent.raw)}"]
        return [f"https://s.weibo.com/weibo?q={quote(intent.raw)}"]

    def _target(self, platform: Platform, intent: Intent) -> str:
        return self._targets(platform, intent)[0]

    def _command(self, platform: Platform, intent: Intent, limit: int) -> list[str]:
        binary = self.binary if Path(self.binary).exists() else shutil.which(self.binary) or self.binary
        command = [binary, "--dump-json", "--no-download", "-o", "output.jsonl=true", "--range", f"1-{max(1, limit)}"]
        platform_file = self.platform_cookie_files.get(platform)
        platform_browser = self.platform_cookies_from_browser.get(platform)
        if platform_file:
            command.extend(["--cookies", platform_file])
        elif platform_browser:
            command.extend(["--cookies-from-browser", platform_browser])
        elif self.cookies_file:
            command.extend(["--cookies", self.cookies_file])
        elif self.cookies_from_browser:
            command.extend(["--cookies-from-browser", self.cookies_from_browser])
        if self.config_path:
            command.extend(["--config", str(Path(self.config_path).expanduser())])
        command.extend(self._targets(platform, intent))
        return command

    async def search(self, platform: Platform, intent: Intent, limit: int, *, request: SearchRequest | None = None) -> list[ImageCandidate]:
        if platform not in self.platforms:
            raise SourceUnavailable(f"gallery-dl does not support {platform.value} in this integration")
        if not self.status.configured:
            raise SourceUnavailable(self.status.detail)
        self._check_cooldown(platform)
        command = self._command(platform, intent, limit)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_process_spawn_options(),
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._failed(platform, f"timed out after {self.timeout_seconds}s")
            if process is not None:
                await _terminate_process_tree(process)
            raise SourceError(f"gallery-dl timed out after {self.timeout_seconds}s") from exc
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process_tree(process)
            raise
        except OSError as exc:
            self._failed(platform, str(exc))
            raise SourceError(f"gallery-dl process failed to start: {exc}") from exc
        if process.returncode != 0:
            detail = _decode_process_output(stderr).strip()[-1000:]
            self._failed(platform, detail or f"exited with code {process.returncode}")
            raise SourceError(f"gallery-dl exited with code {process.returncode}: {detail}")
        output = _decode_process_output(stdout)
        values = _json_values(output)
        if error := _embedded_error(values):
            self._failed(platform, error)
            raise SourceError(f"gallery-dl {platform.value} request failed: {error}")
        requested_media_type = request.media_type if request else None
        candidates = normalize_source_output(platform, output, self.name, limit, requested_media_type)
        if not candidates:
            media_label = {"images": "images", "videos": "videos"}.get(requested_media_type, "media")
            self._failed(platform, f"no {media_label} returned for {platform.value}")
            raise SourceError(
                f"gallery-dl returned no {media_label} for {platform.value}; "
                "the URL may require an authenticated account or may not support search"
            )
        self._succeeded(platform)
        return candidates

    async def fetch_creator(self, request: CreatorFetchRequest) -> CreatorSourceResult:
        """Fetch an X user's media timeline through gallery-dl.

        X API v2 does not expose a username timeline without a separate user
        lookup and pagination flow. gallery-dl already handles that flow and
        returns authenticated media URLs, so adapt its records into the same
        creator envelope used by the other platforms.
        """
        if request.platform not in (Platform.X, Platform.INSTAGRAM, Platform.XHS):
            raise SourceUnavailable(f"gallery-dl creator retrieval does not support {request.platform.value}")
        target = request.creator_name or request.creator_id or request.profile_url or ""
        target = target.strip().lstrip("@")
        if request.profile_url:
            parts = [part for part in urlparse(request.profile_url).path.split("/") if part]
            target = parts[-1] if request.platform == Platform.XHS and parts else (parts[0] if parts else target)
        if not target:
            raise SourceError("X creator username is required")
        if request.platform == Platform.INSTAGRAM:
            profile_url = request.profile_url or f"https://www.instagram.com/{target}/"
            media_url = profile_url
        elif request.platform == Platform.XHS:
            profile_url = request.profile_url or target
            media_url = profile_url
        else:
            profile_url = f"https://x.com/{target}"
            media_url = f"https://x.com/{target}/media"
        # X requires its media subpage; Instagram and XHS use their profile URL.
        if request.platform == Platform.X:
            media_url = f"https://x.com/{target}/media"
        intent = Intent(raw=media_url, normalized=media_url.lower(), tokens=(target.lower(),), negative_tokens=(), url=media_url, identifier_platform=request.platform.value)
        candidates = await self.search(request.platform, intent, max(request.max_images, request.max_videos or 0, request.max_posts * 10))
        items: list[ImageCandidate] = []
        post_ids: list[str] = []
        seen_posts: set[str] = set()
        media_counts = {"image": 0, "video": 0}
        post_image_counts: dict[str, int] = {}
        image_limit = request.max_images
        video_limit = request.max_videos if request.max_videos is not None else request.max_images
        for item in candidates:
            if request.media_type == "images" and item.media_type != "image":
                continue
            if request.media_type == "videos" and item.media_type != "video":
                continue
            record = item.source_payload.get("record") if isinstance(item.source_payload, dict) else {}
            post_id = str((record or {}).get("tweet_id") or (record or {}).get("id") or item.id)
            if post_id not in seen_posts:
                if len(post_ids) >= request.max_posts:
                    break
                seen_posts.add(post_id)
                post_ids.append(post_id)
            if item.media_type == "image":
                if media_counts["image"] >= image_limit:
                    continue
                if request.per_post_limit is not None and post_image_counts.get(post_id, 0) >= request.per_post_limit:
                    continue
                post_image_counts[post_id] = post_image_counts.get(post_id, 0) + 1
            elif media_counts["video"] >= video_limit:
                continue
            items.append(item.model_copy(update={
                "id": f"{post_id}:{item.media_index or 1}",
                "post_id": post_id,
                "creator_id": target,
                "creator_name": target,
                "permalink": item.permalink or (
                    f"https://x.com/{target}/status/{post_id}" if request.platform == Platform.X else profile_url
                ),
            }))
            media_counts[item.media_type] += 1
            if request.media_type == "images" and media_counts["image"] >= image_limit:
                break
            if request.media_type == "videos" and media_counts["video"] >= video_limit:
                break
            if request.media_type == "all" and media_counts["image"] >= image_limit and media_counts["video"] >= video_limit:
                break
        identity = CreatorIdentity(platform=request.platform, requested_id=target, canonical_id=target, name=target, profile_url=profile_url, source=self.name, matched_by="profile_url")
        return CreatorSourceResult(identity=identity, items=items, posts_fetched=len(post_ids), post_ids=tuple(post_ids), pages_fetched=1, next_cursor=None)


class SourceHub:
    def __init__(self, media_crawler_command: str | None, xhs_downloader_command: str | None, gallery_dl_binary: str, gallery_dl_config: str | None = None, timeout_seconds: int = 120, douyin_source_command: str | None = None, douyin_timeout_seconds: int = 45, gallery_dl_cookies_from_browser: str | None = None, failure_cooldown_seconds: int = 120, verification_path: str | None = None, verification_ttl_seconds: int = 86400, gallery_dl_cookies_file: str | None = None, bilibili_source_command: str | None = None, x_gallery_dl_cookies_from_browser: str | None = None, x_gallery_dl_cookies_file: str | None = None, instagram_gallery_dl_cookies_from_browser: str | None = None, instagram_gallery_dl_cookies_file: str | None = None) -> None:
        verification = SourceVerificationStore(verification_path, verification_ttl_seconds)
        # Each platform owns exactly one source object. This isolates process
        # failures, cookies, cooldowns and verification state, even where two
        # platforms happen to use the same underlying CLI executable.
        self.weibo_source = ExternalJsonSource("weibo-cli", media_crawler_command, (Platform.WEIBO,), timeout_seconds, failure_cooldown_seconds, verification)
        self.xhs_source = ExternalJsonSource("xhs-cli", xhs_downloader_command, (Platform.XHS,), timeout_seconds, failure_cooldown_seconds, verification)
        self.douyin_source = ExternalJsonSource("dy-cli", douyin_source_command, (Platform.DOUYIN,), douyin_timeout_seconds, failure_cooldown_seconds, verification)
        self.bilibili_source = ExternalJsonSource("bilibili-cli", bilibili_source_command, (Platform.BILIBILI,), timeout_seconds, failure_cooldown_seconds, verification)
        self.x_source = GalleryDlSource(
            gallery_dl_binary,
            gallery_dl_config,
            timeout_seconds,
            x_gallery_dl_cookies_from_browser or gallery_dl_cookies_from_browser,
            failure_cooldown_seconds,
            verification,
            x_gallery_dl_cookies_file or gallery_dl_cookies_file,
            name="x-cli",
            platforms=(Platform.X,),
        )
        self.instagram_source = GalleryDlSource(
            gallery_dl_binary,
            gallery_dl_config,
            timeout_seconds,
            instagram_gallery_dl_cookies_from_browser or gallery_dl_cookies_from_browser,
            failure_cooldown_seconds,
            verification,
            instagram_gallery_dl_cookies_file or gallery_dl_cookies_file,
            name="instagram-cli",
            platforms=(Platform.INSTAGRAM,),
        )
        self._platform_sources = {
            Platform.DOUYIN: self.douyin_source,
            Platform.WEIBO: self.weibo_source,
            Platform.X: self.x_source,
            Platform.INSTAGRAM: self.instagram_source,
            Platform.XHS: self.xhs_source,
            Platform.BILIBILI: self.bilibili_source,
        }

    def statuses(self) -> list[dict[str, Any]]:
        return [{"name": status.name, "configured": status.configured, "verified": status.verified, "verified_platforms": list(status.verified_platforms), "ready_platforms": list(status.ready_platforms), "mode": status.mode, "detail": status.detail, "platforms": list(status.platforms), "platform_status": status.platform_status or {}} for status in (self.douyin_source.status, self.weibo_source.status, self.x_source.status, self.instagram_source.status, self.xhs_source.status, self.bilibili_source.status)]

    def session_updated(self, platform: Platform) -> None:
        """Allow an immediate retry with new credentials without claiming a crawl succeeded."""
        source = self._platform_sources.get(platform)
        if isinstance(source, ExternalJsonSource):
            for key in list(source._cooldown_until):
                if key == platform.value or key.startswith(platform.value + ":"):
                    source._cooldown_until.pop(key, None)
            source._last_errors.pop(platform.value, None)
            source._last_error = next(iter(source._last_errors.values()), None)

    async def fetch_creator(self, request: CreatorFetchRequest) -> CreatorSourceResult:
        source = self._platform_sources.get(request.platform)
        if source is None or not source.status.configured:
            raise SourceUnavailable(f"No dedicated CLI configured for {request.platform.value}")
        return await source.fetch_creator(request)

    async def search(self, platform: Platform, intent: Intent, limit: int, *, request: SearchRequest | None = None) -> list[ImageCandidate]:
        source = self._platform_sources.get(platform)
        if source is None or not source.status.configured:
            raise SourceUnavailable(f"No dedicated CLI configured for {platform.value}")
        return await source.search(platform, intent, limit, request=request)
