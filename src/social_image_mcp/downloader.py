from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from pathlib import Path

import httpx
from PIL import Image

from .models import DownloadRecord, ImageCandidate


def _safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._")
    return value[:80] or "image"


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


class ImageDownloader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def download_many(self, items: list[ImageCandidate], output_dir: Path, max_concurrency: int = 5, min_width: int = 0, min_height: int = 0, resume: bool = False) -> list[DownloadRecord]:
        output_dir.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(max_concurrency)
        seen_hashes: set[str] = set()
        seen_perceptual: list[int] = []
        lock = asyncio.Lock()
        manifest = output_dir / "manifest.jsonl"
        existing: dict[tuple, DownloadRecord] = {}
        if resume and manifest.exists():
            for line in manifest.read_text(encoding="utf-8").splitlines():
                try:
                    record = DownloadRecord.model_validate_json(line)
                    if not record.path or not record.sha256:
                        continue
                    path = Path(record.path)
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record.sha256:
                        continue
                    if (record.width or 0) < min_width or (record.height or 0) < min_height:
                        continue
                    existing[(record.platform, record.creator_id, record.candidate_id, record.media_type)] = record
                    seen_hashes.add(record.sha256)
                    if record.perceptual_hash:
                        seen_perceptual.append(int(record.perceptual_hash, 16))
                except (ValueError, OSError):
                    continue

        async def one(item: ImageCandidate) -> DownloadRecord:
            async with semaphore:
                previous = existing.get((item.platform, item.creator_id, item.id, item.media_type))
                if previous:
                    record = previous.model_copy(update={"status": "existing", "image_url": item.image_url})
                else:
                    record = await self._download_one(item, output_dir, seen_hashes, seen_perceptual, lock, min_width, min_height)
                record = record.model_copy(update={"creator_id": item.creator_id, "post_id": item.post_id, "media_index": item.media_index})
                # Persist every completed file, including when a later item times out.
                async with lock:
                    with manifest.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
                return record

        records = await asyncio.gather(*(one(item) for item in items))
        return records

    async def _download_one(self, item: ImageCandidate, output_dir: Path, seen_hashes: set[str], seen_perceptual: list[int], lock: asyncio.Lock, min_width: int, min_height: int) -> DownloadRecord:
        try:
            response = None
            last_error = ""
            for attempt in range(3):
                try:
                    headers = {"User-Agent": "Mozilla/5.0 (social-image-mcp)"}
                    if item.platform.value == "x":
                        # X media hosts occasionally reject clients without a
                        # browser-like referer, even though the URL is public.
                        headers["Referer"] = "https://x.com/"
                    elif item.platform.value == "weibo":
                        # Sina image hosts reject direct requests without a
                        # Weibo page referer, even when the image URL is
                        # public. Use the mobile page because creator results
                        # and share links both resolve through m.weibo.cn.
                        headers["Referer"] = item.permalink or "https://m.weibo.cn/"
                    elif item.platform.value == "other" and item.permalink:
                        headers["Referer"] = item.permalink
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
            if item.media_type == "video":
                if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                    raise ValueError(f"not a video response: {content_type}")
                digest = hashlib.sha256(content).hexdigest()
                extension = "mp4"
                if content_type in {"video/webm", "video/x-matroska"}:
                    extension = "webm" if content_type == "video/webm" else "mkv"
                async with lock:
                    if digest in seen_hashes:
                        return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, media_type="video", sha256=digest, status="duplicate")
                    filename = f"{_safe_name(item.platform.value)}_{_safe_name(item.id)}_{digest[:12]}.{extension}"
                    path = output_dir / filename
                    temporary = path.with_suffix(path.suffix + ".part")
                    temporary.write_bytes(content)
                    temporary.replace(path)
                    seen_hashes.add(digest)
                return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, media_type="video", path=str(path.resolve()), sha256=digest, content_type=content_type, status="downloaded")
            if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                raise ValueError(f"not an image response: {content_type}")
            digest = hashlib.sha256(content).hexdigest()
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width < min_width or height < min_height:
                    return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, sha256=digest, width=width, height=height, status="rejected", error="below minimum dimensions")
                extension = (image.format or "jpg").lower().replace("jpeg", "jpg")
                perceptual = _average_hash(image)
            async with lock:
                if digest in seen_hashes or any(_hash_distance(perceptual, previous) <= 5 for previous in seen_perceptual):
                    return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, sha256=digest, width=width, height=height, status="duplicate")
                filename = f"{_safe_name(item.platform.value)}_{_safe_name(item.id)}_{digest[:12]}.{extension}"
                path = output_dir / filename
                temporary = path.with_suffix(path.suffix + ".part")
                temporary.write_bytes(content)
                temporary.replace(path)
                seen_hashes.add(digest)
                seen_perceptual.append(perceptual)
            return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, path=str(path.resolve()), sha256=digest, perceptual_hash=format(perceptual, "x"), width=width, height=height, content_type=response.headers.get("content-type"), status="downloaded")
        except Exception as exc:  # Keep one bad platform item from cancelling the batch.
            return DownloadRecord(candidate_id=item.id, platform=item.platform, image_url=item.image_url, media_type=item.media_type, status="failed", error=str(exc))
