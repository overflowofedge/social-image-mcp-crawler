from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
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


class ImageDownloader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def download_many(self, items: list[ImageCandidate], output_dir: Path, max_concurrency: int = 5, min_width: int = 0, min_height: int = 0, resume: bool = False) -> list[DownloadRecord]:
        output_dir.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(max_concurrency)
        seen_hashes: dict[tuple[str, str], set[str]] = {}
        seen_perceptual: dict[tuple[str, str], list[int]] = {}
        lock = asyncio.Lock()
        manifest = output_dir / "manifest.jsonl"
        existing: dict[tuple[str, str, str, int, str], DownloadRecord] = {}
        if resume and manifest.exists():
            for line in manifest.read_text(encoding="utf-8").splitlines():
                try:
                    record = DownloadRecord.model_validate_json(line)
                    if not record.path or not record.sha256:
                        continue
                    path = Path(record.path)
                    if not path.is_file() or _file_sha256(path) != record.sha256:
                        continue
                    content_minimum = 160 if record.platform.value == "other" and record.media_type == "image" else 0
                    if (record.width or 0) < max(min_width, content_minimum) or (record.height or 0) < max(min_height, content_minimum):
                        continue
                    existing[_record_key(record)] = record
                    scope = _record_scope(record)
                    seen_hashes.setdefault(scope, set()).add(record.sha256)
                    if record.perceptual_hash:
                        seen_perceptual.setdefault(scope, []).append(int(record.perceptual_hash, 16))
                except (ValueError, OSError):
                    continue

        async def one(item: ImageCandidate) -> DownloadRecord:
            async with semaphore:
                previous = existing.get(_item_key(item))
                if previous:
                    record = previous.model_copy(update={
                        "status": "existing", "image_url": item.image_url,
                        "title": item.title, "published_at": item.published_at,
                    })
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

    async def _download_one(self, item: ImageCandidate, output_dir: Path, seen_hashes: dict[tuple[str, str], set[str]], seen_perceptual: dict[tuple[str, str], list[int]], lock: asyncio.Lock, min_width: int, min_height: int) -> DownloadRecord:
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
                except httpx.HTTPError as exc:
                    response = None
                    last_error = str(exc)
                    if attempt < 2:
                        await asyncio.sleep(0.4 * (attempt + 1))
            if response is None:
                raise RuntimeError(last_error or "request failed")
            content = response.content
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                raise ValueError(f"not an image response: {content_type}")
            digest = hashlib.sha256(content).hexdigest()
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                content_minimum = 160 if item.platform.value == "other" else 0
                if width < max(min_width, content_minimum) or height < max(min_height, content_minimum):
                    return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, sha256=digest, width=width, height=height, status="rejected", error="below minimum dimensions")
                extension = (image.format or "jpg").lower().replace("jpeg", "jpg")
                perceptual = _average_hash(image)
            scope = _scope(item)
            async with lock:
                hashes = seen_hashes.setdefault(scope, set())
                perceptuals = seen_perceptual.setdefault(scope, [])
                if digest in hashes or any(_hash_distance(perceptual, previous) <= 5 for previous in perceptuals):
                    return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, sha256=digest, width=width, height=height, status="duplicate")
                filename = _filename(item, extension)
                path = _unique_path(_media_dir(output_dir, item) / filename)
                temporary = path.with_suffix(path.suffix + ".part")
                temporary.write_bytes(content)
                temporary.replace(path)
                hashes.add(digest)
                perceptuals.append(perceptual)
            return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, path=str(path.resolve()), sha256=digest, perceptual_hash=format(perceptual, "x"), width=width, height=height, content_type=response.headers.get("content-type"), status="downloaded")
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
        for attempt in range(3):
            temporary: Path | None = None
            try:
                async with self.client.stream(
                    "GET", item.image_url, headers=headers, follow_redirects=True,
                    timeout=httpx.Timeout(120.0, connect=10.0),
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                        raise ValueError(f"not a video response: {content_type}")
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
                    digest_builder = hashlib.sha256()
                    size = 0
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            digest_builder.update(chunk)
                            size += len(chunk)
                    if size <= 0:
                        raise ValueError("empty video response")
                    digest = digest_builder.hexdigest()
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
                        path=str(path.resolve()), sha256=digest,
                        width=item.width, height=item.height,
                        content_type=content_type, status="downloaded",
                    )
            except asyncio.CancelledError:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
            except httpx.HTTPError as exc:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
            except Exception:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
        raise RuntimeError(last_error or "video request failed")

    async def _stream_track(
        self,
        urls: list[str],
        path: Path,
        headers: dict[str, str],
        media_kind: str,
    ) -> tuple[str, str]:
        last_error = ""
        for url in list(dict.fromkeys(urls)):
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
                        with path.open("wb") as handle:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                                    size += len(chunk)
                        if size <= 0:
                            raise ValueError(f"empty {media_kind} response")
                        return content_type, url
                except asyncio.CancelledError:
                    path.unlink(missing_ok=True)
                    raise
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    path.unlink(missing_ok=True)
                    last_error = str(exc)
                    if attempt == 0:
                        await asyncio.sleep(0.4)
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
            digest = _file_sha256(merged)
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
                path=str(path.resolve()), sha256=digest,
                width=item.width, height=item.height,
                content_type="video/x-matroska", status="downloaded",
            )
        finally:
            video_part.unlink(missing_ok=True)
            audio_part.unlink(missing_ok=True)
            merged.unlink(missing_ok=True)
