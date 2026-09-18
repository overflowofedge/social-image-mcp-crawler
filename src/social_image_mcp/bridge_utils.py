from __future__ import annotations

"""Shared normalization helpers for the optional source-project bridges.

The source projects intentionally keep their native response shapes.  This module
only extracts public image/video URLs and descriptive metadata; it never downloads media.
"""

import html
import re
from collections.abc import Iterable
from typing import Any


_URL_RE = re.compile(r"^https?://", re.I)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return html.unescape(str(value)).strip()
    return ""


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _url(value: Any) -> str | None:
    text = _as_text(value)
    return text if _URL_RE.match(text) else None


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if _URL_RE.match(value)))


def _urls_from_items(items: Any, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            if (value := _url(item)):
                result.append(value)
            continue
        if not isinstance(item, dict):
            continue
        # Source projects expose several qualities for the same image. Keep the
        # first available quality instead of emitting the same image repeatedly.
        for key in keys:
            value = item.get(key)
            if isinstance(value, list):
                values = [v for v in (_url(x) for x in value) if v]
                if values:
                    result.append(values[-1])
                    break
            elif isinstance(value, dict):
                values = _urls_from_mapping(value)
                if values:
                    result.append(values[-1])
                    break
            elif (value := _url(value)):
                result.append(value)
                break
    return _dedupe(result)


def _urls_from_mapping(value: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key, child in value.items():
        key_lower = key.lower()
        if key_lower in {
            "url",
            "url_default",
            "urldefault",
            "original_url",
            "originalurl",
            "display_url",
            "displayurl",
            "large_url",
            "largeurl",
            "master_url",
            "masterurl",
            "url_list",
            "urllist",
        }:
            if isinstance(child, list):
                values = [v for v in (_url(x) for x in child) if v]
                if values:
                    result.append(values[-1])
            elif (item := _url(child)):
                result.append(item)
        elif isinstance(child, dict):
            result.extend(_urls_from_mapping(child))
    return _dedupe(result)


def extract_media_urls(platform: str, record: Any, include_video_covers: bool = True) -> list[str]:
    """Extract high-resolution image URLs from a native source record."""
    if not isinstance(record, dict):
        return []

    if platform == "xhs":
        images = record.get("image_list") or record.get("imageList") or []
        urls = _urls_from_items(images, ("url_default", "urlDefault", "url", "original_url"))
        return urls or _urls_from_mapping(record)

    if platform == "douyin":
        # Douyin has used several field names for image-text posts over time.
        # Keep all known variants so a schema change does not turn a valid
        # result into an empty candidate list.
        images = (
            record.get("images")
            or record.get("image_list")
            or record.get("imageList")
            or record.get("image_infos")
            or record.get("imageInfos")
            or []
        )
        urls = _urls_from_items(images, ("url_list", "urlList", "origin_url", "url"))
        # Video posts still have a useful cover image when no gallery exists.
        if not urls and include_video_covers:
            video = record.get("video") or {}
            for key in ("raw_cover", "origin_cover", "dynamic_cover"):
                urls.extend(_urls_from_mapping(video.get(key) or {}))
        return _dedupe(urls)

    if platform == "weibo":
        source = record.get("mblog") if isinstance(record.get("mblog"), dict) else record
        pics = source.get("pics") or []
        urls = _urls_from_items(pics, ("large", "original", "url", "large_url", "original_url"))
        return urls or _urls_from_mapping(source)

    return _urls_from_mapping(record)


def extract_video_urls(platform: str, record: Any) -> list[str]:
    """Extract original video URLs without treating covers as videos."""
    if not isinstance(record, dict):
        return []
    if platform == "douyin":
        video = record.get("video") or {}
        urls: list[str] = []

        def best(value: Any) -> list[str]:
            if isinstance(value, dict):
                for key in ("url_list", "urlList", "urls"):
                    child = value.get(key)
                    if isinstance(child, list):
                        values = [item for item in (_url(x) for x in child) if item]
                        if values:
                            return [values[-1]]
                    elif (item := _url(child)):
                        return [item]
                return [item for child in value.values() for item in best(child)]
            if isinstance(value, list):
                return [item for child in value for item in best(child)]
            return [_url(value)] if _url(value) else []

        for key in ("play_addr", "download_addr", "play_url", "download_url", "bit_rate", "bitRate"):
            urls = _dedupe(best(video.get(key)))
            if urls:
                return urls[-1:]
        return []
    if platform == "weibo":
        source = record.get("mblog") if isinstance(record.get("mblog"), dict) else record
        page_info = source.get("page_info") or {}
        media_info = page_info.get("media_info") if isinstance(page_info, dict) else {}
        values: list[str] = []
        if isinstance(media_info, dict):
            for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_sd_mp4", "video_url", "stream_url", "h5_url", "url"):
                value = media_info.get(key)
                if isinstance(value, dict):
                    values.extend(_urls_from_mapping(value))
                elif (url := _url(value)):
                    values.append(url)
        return _dedupe(values)
    return []


def normalize_native_record(
    platform: str,
    record: Any,
    source: str,
    *,
    include_video_covers: bool = True,
    media_type: str = "images",
) -> list[dict[str, Any]]:
    """Convert a native record into one JSON candidate per requested media URL."""
    if not isinstance(record, dict):
        return []

    if platform == "xhs":
        item_id = _as_text(_first(record, "note_id", "noteId", "id"))
        title = _as_text(_first(record, "title", "desc"))
        author_data = record.get("user") or {}
        author = _as_text(_first(author_data, "nickname", "nickName", "user_id")) if isinstance(author_data, dict) else ""
        creator_id = _as_text(_first(author_data, "user_id", "userId", "id")) if isinstance(author_data, dict) else ""
        permalink = _as_text(_first(record, "note_url", "permalink", "url"))
        published_at = _first(record, "time", "last_update_time")
    elif platform == "douyin":
        item_id = _as_text(_first(record, "aweme_id", "awemeId", "id"))
        title = _as_text(_first(record, "desc", "title"))
        author_data = record.get("author") or {}
        author = _as_text(_first(author_data, "nickname", "unique_id", "uid")) if isinstance(author_data, dict) else ""
        creator_id = _as_text(_first(author_data, "sec_uid", "secUid", "uid", "unique_id")) if isinstance(author_data, dict) else ""
        permalink = _as_text(_first(record, "aweme_url", "permalink", "url"))
        published_at = _first(record, "create_time", "published_at")
    elif platform == "weibo":
        mblog = record.get("mblog") if isinstance(record.get("mblog"), dict) else record
        item_id = _as_text(_first(mblog, "id", "mid", "mblogid"))
        title = re.sub(r"<[^>]+>", "", _as_text(_first(mblog, "text", "content", "title")))
        author_data = mblog.get("user") or {}
        author = _as_text(_first(author_data, "screen_name", "nickname", "id")) if isinstance(author_data, dict) else ""
        creator_id = _as_text(_first(author_data, "id", "idstr", "uid")) if isinstance(author_data, dict) else ""
        permalink = _as_text(_first(mblog, "url", "permalink")) or (f"https://m.weibo.cn/detail/{item_id}" if item_id else "")
        published_at = _first(mblog, "created_at", "timestamp")
    else:
        item_id = _as_text(_first(record, "id", "item_id", "itemId"))
        title = _as_text(_first(record, "title", "description", "desc", "text"))
        author = _as_text(_first(record, "author", "username"))
        creator_id = _as_text(_first(record, "creator_id", "author_id", "user_id"))
        permalink = _as_text(_first(record, "permalink", "url"))
        published_at = _first(record, "published_at", "created_at", "timestamp")

    image_urls = [] if media_type == "videos" else extract_media_urls(platform, record, include_video_covers=include_video_covers)
    video_urls = [] if media_type == "images" else extract_video_urls(platform, record)
    entries = [(url, "image") for url in image_urls] + [(url, "video") for url in video_urls]
    entries = list(dict.fromkeys(entries))
    if not entries:
        return []
    item_id = item_id or "unknown"
    cover_urls = extract_media_urls(platform, record, include_video_covers=True)
    multiple_images = len(entries) > 1
    return [
        {
            "id": f"{item_id}:{index}" if multiple_images else item_id,
            "post_id": item_id,
            "media_index": index,
            "image_url": media_url,
            "media_type": kind,
            "thumbnail_url": cover_urls[0] if kind == "video" and cover_urls else None,
            "title": title,
            "description": title,
            "author": author,
            "creator_id": creator_id or None,
            "creator_name": author,
            "permalink": permalink or None,
            "published_at": str(published_at) if published_at not in (None, "") else None,
            "engagement_score": _engagement_score(platform, record),
            "source": source,
        }
        for index, (media_url, kind) in enumerate(entries, start=1)
    ]


def _engagement_score(platform: str, record: dict[str, Any]) -> float:
    """Return a stable, non-negative popularity signal from native counters."""
    source = record.get("mblog") if platform == "weibo" and isinstance(record.get("mblog"), dict) else record
    statistics = source.get("statistics") if isinstance(source.get("statistics"), dict) else {}
    values = [
        _first(source, "attitudes_count", "digg_count", "like_count", "liked_count"),
        _first(source, "comments_count", "comment_count"),
        _first(source, "reposts_count", "share_count", "collect_count"),
        _first(statistics, "digg_count", "comment_count", "share_count", "collect_count"),
    ]
    total = 0.0
    for value in values:
        try:
            total += max(0.0, float(value or 0))
        except (TypeError, ValueError):
            continue
    return total
