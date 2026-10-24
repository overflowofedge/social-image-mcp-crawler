from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image

from .models import DownloadRecord, ImageCandidate


def _safe_name(value: str) -> str:
    """Make a Windows-safe name while retaining Chinese and other Unicode text."""
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    # Windows rejects these device names even when an extension is present.
    if value.upper().split(".", 1)[0] in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        value = f"_{value}"
    return value[:80] or "untitled"


def _timestamp_label(value: str | None) -> str:
    """Return a stable sortable timestamp for a creator work."""
    if value:
        text = str(value).strip()
        try:
            numeric = float(text)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, timezone.utc).strftime("%Y%m%d_%H%M%S")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
            except (TypeError, ValueError, OverflowError):
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).strftime("%Y%m%d_%H%M%S")
                except ValueError:
                    continue
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _scope(item: ImageCandidate) -> tuple[str, str]:
    return item.platform.value, item.media_type


def _record_scope(record: DownloadRecord) -> tuple[str, str]:
    return record.platform.value, record.media_type


def _item_key(item: ImageCandidate) -> tuple[str, str, str, int, str]:
    """Use work identity instead of a signed CDN URL for resume/deduplication."""
    return (
        item.platform.value,
        item.creator_id or "",
        item.post_id or item.id,
        item.media_index or 1,
        item.media_type,
    )


def _record_key(record: DownloadRecord) -> tuple[str, str, str, int, str]:
    return (
        record.platform.value,
        record.creator_id or "",
        record.post_id or record.candidate_id,
        record.media_index or 1,
        record.media_type,
    )


def _media_dir(output_dir: Path, item: ImageCandidate) -> Path:
    # Creator jobs already use a platform-scoped output directory. Keeping the
    # media folder directly below it avoids paths such as
    # creators/douyin/name/douyin/images. Search jobs use the same images/videos
    # folders below downloads, so every entry point follows one predictable rule.
    path = output_dir / ("videos" if item.media_type == "video" else "images")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _filename(item: ImageCandidate, extension: str) -> str:
    work_id = _safe_name(item.post_id or item.id)
    title = _safe_name(item.title or "untitled")
    index = f"_{item.media_index:02d}" if item.media_index and item.media_index > 1 else ""
    return f"{_timestamp_label(item.published_at)}_{work_id}_{title}{index}.{extension}"


def _unique_path(path: Path) -> Path:
    """Avoid overwriting a different work with the same display title."""
    if not path.exists() and not path.with_suffix(path.suffix + ".part").exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(2, 10_000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists() and not candidate.with_suffix(candidate.suffix + ".part").exists():
            return candidate
    raise RuntimeError(f"too many files with the same name: {path.name}")


def _average_hash(image: Image.Image, size: int = 16) -> int:
    gray = image.convert("L").resize((size, size))
    flattened = getattr(gray, "get_flattened_data", None)
    pixels = list(flattened() if flattened else gray.getdata())
    average = sum(pixels) / max(len(pixels), 1)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= average)
    return value


def _hash_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches_sha256(path: Path, expected: str | None, expected_size: int | None) -> bool:
    """Verify a manifest file without re-reading every byte on every resume.

    A stored size acts as the cheap integrity gate: only when the file size
    is missing from an old record or has drifted do we fall back to hashing
    the whole file once. After that run the rewritten manifest line carries
    ``size_bytes`` and later resumes stay O(1) per file.
    """
    if not path.is_file():
        return False
    if expected_size is not None:
        try:
            return path.stat().st_size == expected_size
        except OSError:
            return False
    return bool(expected) and _file_sha256(path) == expected


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Wait longer when the server signals throttling instead of a blip."""
    if response is not None and response.status_code in (429, 503):
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(30.0, max(1.0, float(retry_after)))
            except ValueError:
                pass
        return 2.0 * (2 ** attempt)
    return 0.4 * (attempt + 1)


class _StreamTooSlow(Exception):
    """Raised to abandon a media stream whose throughput collapsed.

    CDN edges throttle long bulk pulls per IP instead of failing them; the
    request never errors, it just crawls. Abandoning and re-opening on
    another mirror is much faster than waiting out the throttle.
    """


_MIN_STREAM_SPEED = int(os.getenv("MIN_STREAM_SPEED_KB_S", "200")) * 1024
_STREAM_SPEED_GRACE_SECONDS = 8.0

_UPOS_MIRRORS = (
    "upos-sz-mirrorcosov.bilivideo.com",
    "upos-sz-mirror08c.bilivideo.com",
    "upos-sz-mirrorcosbstarv2.bilivideo.com",
    "upos-sz-mirroraliov.bilivideo.com",
)


def _stream_url_variants(urls: list[str]) -> list[str]:
    """Expand each media URL with its upos mirror alternates.

    Bilibili resources live on several mirror hosts that rate-limit
    independently, so a stalled original can resume full speed elsewhere.
    Non-upos URLs pass through untouched and keep their original order.
    """
    variants: list[str] = []
    for url in urls:
        variants.append(url)
        parts = urlparse(url)
        hostname = (parts.hostname or "").lower()
        if not hostname.endswith(".bilivideo.com"):
            continue
        for mirror in _UPOS_MIRRORS:
            if hostname != mirror:
                variants.append(f"https://{mirror}{parts.path}?{parts.query}")
    return list(dict.fromkeys(variants))


class _PerceptualIndex:
    """Hamming neighbor lookup for 256-bit average hashes.

    Each hash splits into eight 32-bit chunks. Two hashes within distance 5
    can corrupt at most five chunks, so at least three chunks are identical
    (pigeonhole). Exact-match lookup on any chunk therefore surfaces every
    true neighbor, and only that shortlist needs a full distance check —
    O(1) per comparison instead of scanning every stored hash.
    """

    CHUNKS = 8
    CHUNK_BITS = 32
    _MASK = (1 << CHUNK_BITS) - 1

    def __init__(self) -> None:
        self._buckets: list[dict[int, list[tuple[int, str]]]] = [{} for _ in range(self.CHUNKS)]

    def add(self, hash_value: int, work_id: str) -> None:
        for index in range(self.CHUNKS):
            chunk = (hash_value >> (index * self.CHUNK_BITS)) & self._MASK
            self._buckets[index].setdefault(chunk, []).append((hash_value, work_id))

    def has_near(self, hash_value: int, work_id: str, max_distance: int = 5) -> bool:
        checked: set[int] = set()
        for index in range(self.CHUNKS):
            chunk = (hash_value >> (index * self.CHUNK_BITS)) & self._MASK
            for candidate_hash, candidate_work in self._buckets[index].get(chunk, ()):
                if candidate_hash in checked:
                    continue
                checked.add(candidate_hash)
                if candidate_work != work_id and _hash_distance(hash_value, candidate_hash) <= max_distance:
                    return True
        return False


class ImageDownloader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def download_many(self, items: list[ImageCandidate], output_dir: Path, max_concurrency: int = 5, min_width: int = 0, min_height: int = 0, resume: bool = False) -> list[DownloadRecord]:
        output_dir.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(max_concurrency)
        seen_hashes: dict[tuple[str, str], set[str]] = {}
        # Perceptual neighbors live in a chunked index; a carousel/gallery is
        # allowed to contain visually similar frames, so near-duplicates are
        # only meaningful across different works.
        seen_perceptual: dict[tuple[str, str], _PerceptualIndex] = {}
        lock = asyncio.Lock()
        manifest = output_dir / "manifest.jsonl"
        existing: dict[tuple[str, str, str, int, str], DownloadRecord] = {}
        if resume and manifest.exists():
            lines = await asyncio.to_thread(manifest.read_text, "utf-8")
            for line in lines.splitlines():
                try:
                    record = DownloadRecord.model_validate_json(line)
                    if not record.path or not record.sha256:
                        continue
                    path = Path(record.path)
                    if not await asyncio.to_thread(_file_matches_sha256, path, record.sha256, record.size_bytes):
                        continue
                    content_minimum = 160 if record.platform.value == "other" and record.media_type == "image" else 0
                    if (record.width or 0) < max(min_width, content_minimum) or (record.height or 0) < max(min_height, content_minimum):
                        continue
                    existing[_record_key(record)] = record
                    scope = _record_scope(record)
                    seen_hashes.setdefault(scope, set()).add(record.sha256)
                    if record.perceptual_hash:
                        index = seen_perceptual.setdefault(scope, _PerceptualIndex())
                        index.add(int(record.perceptual_hash, 16), record.post_id or record.candidate_id)
                except (ValueError, OSError):
                    continue

        async def one(item: ImageCandidate) -> DownloadRecord:
            async with semaphore:
                previous = existing.get(_item_key(item))
                if previous:
                    updates: dict[str, Any] = {
                        "status": "existing", "image_url": item.image_url,
                        "title": item.title, "published_at": item.published_at,
                    }
                    if previous.size_bytes is None and previous.path:
                        # Backfill legacy manifests so the next resume takes
                        # the size-gated fast path instead of hashing.
                        try:
                            updates["size_bytes"] = Path(previous.path).stat().st_size
                        except OSError:
                            pass
                    record = previous.model_copy(update=updates)
                else:
                    record = await self._download_one(item, output_dir, seen_hashes, seen_perceptual, lock, min_width, min_height)
                record = record.model_copy(update={
                    "creator_id": item.creator_id, "post_id": item.post_id,
                    "media_index": item.media_index, "title": item.title,
                    "published_at": item.published_at,
                })
                # Persist every completed file, including when a later item times out.
                async with lock:
                    with manifest.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
                return record

        records = await asyncio.gather(*(one(item) for item in items))
        return records

    async def _download_one(self, item: ImageCandidate, output_dir: Path, seen_hashes: dict[tuple[str, str], set[str]], seen_perceptual: dict[tuple[str, str], _PerceptualIndex], lock: asyncio.Lock, min_width: int, min_height: int) -> DownloadRecord:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (social-image-mcp)"}
            if item.platform.value == "x":
                headers["Referer"] = "https://x.com/"
            elif item.platform.value == "weibo":
                headers["Referer"] = item.permalink or "https://m.weibo.cn/"
            elif item.platform.value == "douyin":
                headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
                headers["Referer"] = item.permalink or "https://www.douyin.com/"
                headers["Origin"] = "https://www.douyin.com"
            elif item.platform.value == "bilibili":
                headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
                headers["Referer"] = item.permalink or "https://www.bilibili.com/"
                headers["Origin"] = "https://www.bilibili.com"
            elif item.platform.value == "other" and item.permalink:
                headers["Referer"] = item.permalink
            if item.media_type == "video":
                return await self._download_video(item, output_dir, seen_hashes, lock, headers)

            response = None
            last_error = ""
            for attempt in range(3):
                try:
                    response = await self.client.get(item.image_url, headers=headers, follow_redirects=True, timeout=30)
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    response = None
                    last_error = str(exc)
                    # Hotlink-protected CDNs answer a page-referred request
                    # with 403; a Referer-less retry is the standard fix.
                    if exc.response.status_code in (401, 403) and "Referer" in headers:
                        headers = {key: value for key, value in headers.items() if key != "Referer"}
                        continue
                    if attempt < 2:
                        await asyncio.sleep(_retry_delay(exc.response, attempt))
                except httpx.HTTPError as exc:
                    response = None
                    last_error = str(exc)
                    if attempt < 2:
                        await asyncio.sleep(_retry_delay(getattr(exc, "response", None), attempt))
            if response is None:
                raise RuntimeError(last_error or "request failed")
            content = response.content
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                raise ValueError(f"not an image response: {content_type}")
            digest = hashlib.sha256(content).hexdigest()

            def inspect_image() -> tuple[int, int, str, int]:
                # verify() plus a second parse and the average hash are pure
                # CPU work over megabytes; keep them off the event loop so
                # concurrent downloads keep flowing while images decode.
                with Image.open(io.BytesIO(content)) as image:
                    image.verify()
                with Image.open(io.BytesIO(content)) as image:
                    width, height = image.size
                    extension = (image.format or "jpg").lower().replace("jpeg", "jpg")
                    perceptual = _average_hash(image)
                return width, height, extension, perceptual

            width, height, extension, perceptual = await asyncio.to_thread(inspect_image)
            content_minimum = 160 if item.platform.value == "other" else 0
            if width < max(min_width, content_minimum) or height < max(min_height, content_minimum):
                return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, sha256=digest, width=width, height=height, status="rejected", error="below minimum dimensions")
            scope = _scope(item)
            async with lock:
                hashes = seen_hashes.setdefault(scope, set())
                index = seen_perceptual.setdefault(scope, _PerceptualIndex())
                work_id = item.post_id or item.id
                # Exact bytes are duplicates regardless of origin.  Similar
                # pixels, however, must not collapse separate media in the
                # same post (Instagram carousels commonly reuse a layout or
                # background across slides).
                near_duplicate = index.has_near(perceptual, work_id)
                if digest in hashes or near_duplicate:
                    return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, sha256=digest, width=width, height=height, status="duplicate")
                filename = _filename(item, extension)
                path = _unique_path(_media_dir(output_dir, item) / filename)
                temporary = path.with_suffix(path.suffix + ".part")
                temporary.write_bytes(content)
                temporary.replace(path)
                hashes.add(digest)
                index.add(perceptual, work_id)
            return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, path=str(path.resolve()), sha256=digest, size_bytes=len(content), perceptual_hash=format(perceptual, "x"), width=width, height=height, content_type=response.headers.get("content-type"), status="downloaded")
        except Exception as exc:  # Keep one bad platform item from cancelling the batch.
            return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, media_type=item.media_type, status="failed", error=str(exc))

    async def _download_video(
        self,
        item: ImageCandidate,
        output_dir: Path,
        seen_hashes: dict[tuple[str, str], set[str]],
        lock: asyncio.Lock,
        headers: dict[str, str],
    ) -> DownloadRecord:
        """Download a complete file, or mux the selected best video/audio tracks."""
        download = item.source_payload.get("download") if isinstance(item.source_payload, dict) else {}
        download = download if isinstance(download, dict) else {}
        video_urls = [item.image_url]
        if isinstance(download.get("video_urls"), list):
            video_urls.extend(str(value) for value in download["video_urls"] if value)
        video_urls = list(dict.fromkeys(video_urls))
        audio_urls: list[str] = []
        if download.get("audio_url"):
            audio_urls.append(str(download["audio_url"]))
        if isinstance(download.get("audio_urls"), list):
            audio_urls.extend(str(value) for value in download["audio_urls"] if value)
        audio_urls = list(dict.fromkeys(audio_urls))
        fallback_url = str(download.get("fallback_url") or "")

        if audio_urls:
            executable = self._ffmpeg_executable()
            if executable:
                try:
                    return await self._download_muxed_video(
                        item, output_dir, seen_hashes, lock, headers,
                        video_urls, audio_urls, executable,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not fallback_url:
                        raise
            elif not fallback_url:
                raise RuntimeError("FFmpeg is required to combine the selected highest-quality video and audio tracks")
            item = item.model_copy(update={"image_url": fallback_url})

        return await self._download_direct_video(item, output_dir, seen_hashes, lock, headers)

    @staticmethod
    def _ffmpeg_executable() -> str | None:
        if executable := shutil.which("ffmpeg"):
            return executable
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
            return executable if executable and Path(executable).is_file() else None
        except (ImportError, OSError, RuntimeError):
            return None

    async def _download_direct_video(
        self,
        item: ImageCandidate,
        output_dir: Path,
        seen_hashes: dict[tuple[str, str], set[str]],
        lock: asyncio.Lock,
        headers: dict[str, str],
    ) -> DownloadRecord:
        """Stream a self-contained video file without loading it into memory."""
        last_error = ""
        temporary: Path | None = None
        path: Path | None = None
        content_type = ""
        for attempt in range(3):
            try:
                offset = temporary.stat().st_size if temporary and temporary.exists() else 0
                request_headers = {**headers, "Accept-Encoding": "identity"}
                if offset:
                    request_headers["Range"] = f"bytes={offset}-"
                async with self.client.stream(
                    "GET", item.image_url, headers=request_headers, follow_redirects=True,
                    timeout=httpx.Timeout(120.0, connect=10.0),
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    accepted_video_type = (
                        content_type.startswith("video/")
                        or content_type in {"application/octet-stream", "application/mp4"}
                        # Douyin play URLs are video candidates but some CDN
                        # edges label the complete MP4 container as audio/mp4.
                        or (item.platform.value == "douyin" and content_type == "audio/mp4")
                    )
                    if content_type and not accepted_video_type:
                        raise ValueError(f"not a video response: {content_type}")
                    if temporary is None:
                        suffix = Path(urlparse(item.image_url).path).suffix.lower().lstrip(".")
                        extension = {
                            "video/mp4": "mp4", "video/webm": "webm", "video/x-matroska": "mkv",
                            "video/quicktime": "mov", "video/ogg": "ogv", "video/x-m4v": "m4v",
                        }.get(content_type, suffix if suffix in {"mp4", "webm", "mkv", "mov", "ogv", "ogg", "m4v"} else "mp4")
                        async with lock:
                            filename = _filename(item, extension)
                            path = _unique_path(_media_dir(output_dir, item) / filename)
                            temporary = path.with_suffix(path.suffix + ".part")
                            temporary.touch(exist_ok=False)
                    resumed = offset > 0 and response.status_code == 206
                    if offset > 0 and not resumed:
                        offset = 0
                    mode = "ab" if resumed else "wb"
                    size = offset
                    started = time.monotonic()
                    with temporary.open(mode) as handle:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            handle.write(chunk)
                            size += len(chunk)
                            elapsed = time.monotonic() - started
                            if elapsed > _STREAM_SPEED_GRACE_SECONDS and (size - offset) / elapsed < _MIN_STREAM_SPEED:
                                raise _StreamTooSlow(f"{size // 1024}KB in {elapsed:.0f}s")
                    if size <= 0:
                        raise ValueError("empty video response")
                    # Hashing gigabytes must not stall the event loop while
                    # other videos in the batch keep streaming.
                    digest = await asyncio.to_thread(_file_sha256, temporary)
                    scope = _scope(item)
                    async with lock:
                        hashes = seen_hashes.setdefault(scope, set())
                        if digest in hashes:
                            temporary.unlink(missing_ok=True)
                            return DownloadRecord(
                                candidate_id=item.id, platform=item.platform,
                                image_url=item.image_url, media_type="video",
                                sha256=digest, status="duplicate",
                            )
                        temporary.replace(path)
                        hashes.add(digest)
                    return DownloadRecord(
                        candidate_id=item.id, platform=item.platform,
                        image_url=item.image_url, media_type="video",
                        path=str(path.resolve()), sha256=digest, size_bytes=size,
                        width=item.width, height=item.height,
                        content_type=content_type, status="downloaded",
                    )
            except asyncio.CancelledError:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
            except (httpx.HTTPError, _StreamTooSlow) as exc:
                last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(_retry_delay(getattr(exc, "response", None), attempt))
            except Exception:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RuntimeError(last_error or "video request failed")

    async def _stream_track(
        self,
        urls: list[str],
        path: Path,
        headers: dict[str, str],
        media_kind: str,
    ) -> tuple[str, str]:
        last_error = ""
        for url in _stream_url_variants(list(dict.fromkeys(urls))):
            for attempt in range(2):
                try:
                    async with self.client.stream(
                        "GET", url, headers=headers, follow_redirects=True,
                        timeout=httpx.Timeout(180.0, connect=10.0),
                    ) as response:
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                        accepted = (
                            content_type in {"application/octet-stream", "application/mp4"}
                            or content_type.startswith(f"{media_kind}/")
                            or (media_kind == "audio" and content_type == "video/mp4")
                        )
                        if content_type and not accepted:
                            raise ValueError(f"not a {media_kind} response: {content_type}")
                        size = 0
                        started = time.monotonic()
                        with path.open("wb") as handle:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                                    size += len(chunk)
                                    # A throttled mirror crawls instead of
                                    # failing; abandon it for the next host.
                                    elapsed = time.monotonic() - started
                                    if elapsed > _STREAM_SPEED_GRACE_SECONDS and size / elapsed < _MIN_STREAM_SPEED:
                                        raise _StreamTooSlow(f"{size // 1024}KB in {elapsed:.0f}s")
                        if size <= 0:
                            raise ValueError(f"empty {media_kind} response")
                        return content_type, url
                except asyncio.CancelledError:
                    path.unlink(missing_ok=True)
                    raise
                except (httpx.HTTPError, OSError, ValueError, _StreamTooSlow) as exc:
                    path.unlink(missing_ok=True)
                    last_error = str(exc)
                    if attempt == 0:
                        await asyncio.sleep(_retry_delay(getattr(exc, "response", None), attempt))
        raise RuntimeError(last_error or f"{media_kind} track request failed")

    async def _mux_streams(self, executable: str, video: Path, audio: Path, output: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
            "-f", "matroska", str(output),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
            raise RuntimeError(f"FFmpeg failed to combine video and audio: {detail or process.returncode}")

    async def _download_muxed_video(
        self,
        item: ImageCandidate,
        output_dir: Path,
        seen_hashes: dict[tuple[str, str], set[str]],
        lock: asyncio.Lock,
        headers: dict[str, str],
        video_urls: list[str],
        audio_urls: list[str],
        executable: str,
    ) -> DownloadRecord:
        async with lock:
            filename = _filename(item, "mkv")
            path = _unique_path(_media_dir(output_dir, item) / filename)
            merged = path.with_suffix(path.suffix + ".part")
            merged.touch(exist_ok=False)
        video_part = path.with_suffix(path.suffix + ".video.part")
        audio_part = path.with_suffix(path.suffix + ".audio.part")
        try:
            await self._stream_track(video_urls, video_part, headers, "video")
            await self._stream_track(audio_urls, audio_part, headers, "audio")
            await self._mux_streams(executable, video_part, audio_part, merged)
            if not merged.is_file() or merged.stat().st_size <= 0:
                raise RuntimeError("FFmpeg produced an empty media file")
            merged_size = merged.stat().st_size
            digest = await asyncio.to_thread(_file_sha256, merged)
            scope = _scope(item)
            async with lock:
                hashes = seen_hashes.setdefault(scope, set())
                if digest in hashes:
                    merged.unlink(missing_ok=True)
                    return DownloadRecord(
                        candidate_id=item.id, platform=item.platform,
                        image_url=item.image_url, media_type="video",
                        sha256=digest, width=item.width, height=item.height,
                        status="duplicate",
                    )
                merged.replace(path)
                hashes.add(digest)
            return DownloadRecord(
                candidate_id=item.id, platform=item.platform,
                image_url=item.image_url, media_type="video",
                path=str(path.resolve()), sha256=digest, size_bytes=merged_size,
                width=item.width, height=item.height,
                content_type="video/x-matroska", status="downloaded",
            )
        finally:
            video_part.unlink(missing_ok=True)
            audio_part.unlink(missing_ok=True)
            merged.unlink(missing_ok=True)
