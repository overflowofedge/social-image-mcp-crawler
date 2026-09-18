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
from .models import CreatorFetchRequest, CreatorIdentity, ImageCandidate, Platform


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
    values = [
        metadata.get("media_type"), metadata.get("mimetype"), metadata.get("mime_type"),
        metadata.get("content_type"), metadata.get("extension"), metadata.get("ext"),
        metadata.get("filename"), image_url,
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
        media_index=max(1, int(_number(_first(metadata, "media_index", "image_index", "imageIndex"), 1))),
        engagement_score=max(0.0, _number(_first(metadata, "engagement_score", "engagementScore"), 0)),
        source_payload={"source": source_name, "record": metadata},
    )


def normalize_source_output(platform: Platform, output: str, source_name: str, limit: int) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    for value in _json_values(output):
        for index, record in enumerate(_records(value)):
            # gallery-dl's [type, media_url, metadata] uses metadata.url for
            # the source page. Types 2 and 6 are directory/queue messages,
            # not downloadable images; only type 3 is actual media.
            if isinstance(record, list) and record and isinstance(record[0], int):
                if record[0] != 3 or len(record) <= 1:
                    continue
                image_urls = _urls(record[1])
            else:
                image_urls = _urls(record)
            for image_url in image_urls:
                candidates.append(_candidate(platform, record, image_url, len(candidates) + index, source_name))
                if len(candidates) >= limit:
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
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
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
            detail = f"{self.name} creator request timed out after {self.timeout_seconds}s"
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

    async def search(self, platform: Platform, intent: Intent, limit: int) -> list[ImageCandidate]:
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
            # Windows defaults to the active code page (often GBK).  Source
            # bridges emit JSON metadata that may contain emoji or other
            # Unicode, so force UTF-8 for both Python and compatible CLIs.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
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
        candidates = normalize_source_output(platform, _decode_process_output(stdout), self.name, limit)
        if not candidates:
            self._failed(platform, f"no image candidates returned for {platform.value}")
            raise SourceError(
                f"{self.name} returned no image candidates for {platform.value}; "
                "check login state, query, and source-project output"
            )
        self._succeeded(platform)
        return candidates


class GalleryDlSource:
    """gallery-dl bridge for X/Instagram/Weibo URLs and search pages."""

    def __init__(self, binary: str = "gallery-dl", config_path: str | None = None, timeout_seconds: int = 120, cookies_from_browser: str | None = None, failure_cooldown_seconds: int = 120, verification_store: SourceVerificationStore | None = None, cookies_file: str | None = None) -> None:
        self.binary = binary
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds
        self.cookies_from_browser = cookies_from_browser
        self.cookies_file = str(Path(cookies_file).expanduser()) if cookies_file else None
        self.failure_cooldown_seconds = max(0, failure_cooldown_seconds)
        self.platforms = (Platform.X, Platform.INSTAGRAM, Platform.WEIBO, Platform.XHS)
        self._verified = False
        self._last_error: str | None = None
        self._verified_platforms: set[str] = set()
        self._last_errors: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._verification = verification_store or SourceVerificationStore()

    @property
    def status(self) -> SourceStatus:
        found = Path(self.binary).exists() or shutil.which(self.binary) is not None
        platform_status = {platform.value: self._verification.status("gallery-dl", platform.value) for platform in self.platforms}
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
            detail = "Last request completed successfully and returned images"
        else:
            detail = "gallery-dl is installed; account access is not verified until a real request"
        return SourceStatus(
            "gallery-dl",
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
        self._verification.record_failure("gallery-dl", platform.value, detail)

    def _succeeded(self, platform: Platform) -> None:
        self._verified_platforms.add(platform.value)
        self._last_errors.pop(platform.value, None)
        self._verified = True
        self._last_error = None
        self._cooldown_until.pop(platform.value, None)
        self._verification.record_success("gallery-dl", platform.value)

    def _check_cooldown(self, platform: Platform) -> None:
        until = self._cooldown_until.get(platform.value, 0.0)
        if until <= time.monotonic():
            self._cooldown_until.pop(platform.value, None)
            return
        remaining = max(1, int(until - time.monotonic()))
        detail = self._last_errors.get(platform.value, "previous request failed")
        raise SourceError(
            f"gallery-dl temporarily skipped for {platform.value} for {remaining}s "
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
        if self.cookies_file:
            command.extend(["--cookies", self.cookies_file])
        elif self.cookies_from_browser:
            command.extend(["--cookies-from-browser", self.cookies_from_browser])
        if self.config_path:
            command.extend(["--config", str(Path(self.config_path).expanduser())])
        command.extend(self._targets(platform, intent))
        return command

    async def search(self, platform: Platform, intent: Intent, limit: int) -> list[ImageCandidate]:
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
        candidates = normalize_source_output(platform, output, "gallery-dl", limit)
        if not candidates:
            self._failed(platform, f"no images returned for {platform.value}")
            raise SourceError(
                f"gallery-dl returned no images for {platform.value}; "
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
            path = urlparse(request.profile_url).path.strip("/")
            target = path.split("/", 1)[0] if path else target
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
        # The bare profile URL is only a timeline directory. The media
        # subpage is required for gallery-dl to emit downloadable records.
        media_url = f"https://x.com/{target}/media"
        intent = Intent(raw=media_url, normalized=media_url.lower(), tokens=(target.lower(),), negative_tokens=(), url=media_url, identifier_platform=request.platform.value)
        candidates = await self.search(request.platform, intent, max(request.max_images, request.max_videos or 0, request.max_posts * 10))
        items: list[ImageCandidate] = []
        post_ids: list[str] = []
        seen_posts: set[str] = set()
        for item in candidates:
            if request.media_type == "images" and item.media_type != "image":
                continue
            if request.media_type == "videos" and item.media_type != "video":
                continue
            record = item.source_payload.get("record") if isinstance(item.source_payload, dict) else {}
            post_id = str((record or {}).get("tweet_id") or (record or {}).get("id") or item.id)
            if post_id not in seen_posts:
                seen_posts.add(post_id)
                post_ids.append(post_id)
            items.append(item.model_copy(update={
                "id": f"{post_id}:{item.media_index or 1}",
                "post_id": post_id,
                "creator_id": target,
                "creator_name": target,
                "permalink": item.permalink or f"https://x.com/{target}/status/{post_id}",
            }))
            if len(items) >= request.max_images:
                break
        identity = CreatorIdentity(platform=request.platform, requested_id=target, canonical_id=target, name=target, profile_url=profile_url, source="gallery-dl", matched_by="profile_url")
        return CreatorSourceResult(identity=identity, items=items, posts_fetched=len(post_ids), post_ids=tuple(post_ids), pages_fetched=1, next_cursor=None)


class SourceHub:
    def __init__(self, media_crawler_command: str | None, xhs_downloader_command: str | None, gallery_dl_binary: str, gallery_dl_config: str | None = None, timeout_seconds: int = 120, douyin_source_command: str | None = None, douyin_timeout_seconds: int = 45, gallery_dl_cookies_from_browser: str | None = None, failure_cooldown_seconds: int = 120, verification_path: str | None = None, verification_ttl_seconds: int = 86400, gallery_dl_cookies_file: str | None = None, douyin_media_crawler_fallback: bool = False) -> None:
        # Douyin is intentionally handled by dy-cli. MediaCrawler's Douyin
        # browser-login path is not used when embedded in an MCP stdio server.
        # MediaCrawler remains available as an opt-in Douyin creator fallback.
        # The dy-cli bridge already has its own browser fallback, so invoking
        # another browser stack by default only duplicates latency after a 403.
        domestic = (Platform.XHS, Platform.WEIBO, Platform.DOUYIN)
        verification = SourceVerificationStore(verification_path, verification_ttl_seconds)
        self.media_crawler = ExternalJsonSource("media-crawler", media_crawler_command, domestic, timeout_seconds, failure_cooldown_seconds, verification)
        self.xhs_downloader = ExternalJsonSource("xhs-downloader", xhs_downloader_command, (Platform.XHS,), timeout_seconds, failure_cooldown_seconds, verification)
        self.douyin_source = ExternalJsonSource("dy-cli", douyin_source_command, (Platform.DOUYIN,), douyin_timeout_seconds, failure_cooldown_seconds, verification)
        self.douyin_media_crawler_fallback = douyin_media_crawler_fallback
        self.gallery_dl = GalleryDlSource(gallery_dl_binary, gallery_dl_config, timeout_seconds, gallery_dl_cookies_from_browser, failure_cooldown_seconds, verification, gallery_dl_cookies_file)

    def statuses(self) -> list[dict[str, Any]]:
        return [{"name": status.name, "configured": status.configured, "verified": status.verified, "verified_platforms": list(status.verified_platforms), "ready_platforms": list(status.ready_platforms), "mode": status.mode, "detail": status.detail, "platforms": list(status.platforms), "platform_status": status.platform_status or {}} for status in (self.douyin_source.status, self.media_crawler.status, self.xhs_downloader.status, self.gallery_dl.status)]

    async def fetch_creator(self, request: CreatorFetchRequest) -> CreatorSourceResult:
        if request.platform == Platform.DOUYIN:
            errors: list[str] = []
            if self.douyin_source.command_template:
                try:
                    return await self.douyin_source.fetch_creator(request)
                except (SourceError, SourceUnavailable) as exc:
                    errors.append(f"dy-cli: {exc}")
            if self.media_crawler.command_template and (self.douyin_media_crawler_fallback or not self.douyin_source.command_template):
                try:
                    return await self.media_crawler.fetch_creator(request)
                except (SourceError, SourceUnavailable) as exc:
                    errors.append(f"media-crawler: {exc}")
            raise SourceError("; ".join(errors) or "No creator source configured for douyin")
        if request.platform == Platform.WEIBO:
            return await self.media_crawler.fetch_creator(request)
        if request.platform == Platform.BILIBILI:
            raise SourceUnavailable("Bilibili creator retrieval uses the native public API")
        if request.platform in (Platform.X, Platform.INSTAGRAM, Platform.XHS):
            return await self.gallery_dl.fetch_creator(request)
        raise SourceUnavailable("creator retrieval currently supports only douyin and weibo")

    async def search(self, platform: Platform, intent: Intent, limit: int) -> list[ImageCandidate]:
        sources: list[Any] = []
        if platform == Platform.DOUYIN and self.douyin_source.command_template:
            sources.append(self.douyin_source)
        if platform in self.media_crawler.platforms and self.media_crawler.command_template and not (platform == Platform.DOUYIN and self.douyin_source.command_template):
            sources.append(self.media_crawler)
        if platform == Platform.XHS and self.xhs_downloader.command_template and (intent.url or intent.identifier):
            sources.append(self.xhs_downloader)
        if platform in self.gallery_dl.platforms and self.gallery_dl.status.configured and (platform != Platform.WEIBO or intent.url or intent.identifier):
            sources.append(self.gallery_dl)
        if not sources:
            raise SourceUnavailable(f"No recommended source configured for {platform.value}; configure MediaCrawler/XHS-Downloader/gallery-dl")
        results = await asyncio.gather(*(source.search(platform, intent, limit) for source in sources), return_exceptions=True)
        candidates: list[ImageCandidate] = []
        errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(str(result))
            else:
                candidates.extend(result)
        if not candidates and errors:
            raise SourceError("; ".join(errors))
        return candidates
