from __future__ import annotations

import json
import re
from typing import Any


_BILIBILI_HEIGHTS = {
    127: 4320,
    126: 2160,
    125: 2160,
    120: 2160,
    116: 1080,
    112: 1080,
    80: 1080,
    74: 720,
    64: 720,
    32: 480,
    16: 360,
}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _search_text(metadata: dict[str, Any], url: str = "", context: str = "") -> str:
    try:
        serialized = json.dumps(metadata, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        serialized = str(metadata)
    return f"{context} {url} {serialized}".lower()


def is_hdr_video(metadata: dict[str, Any], url: str = "", context: str = "") -> bool:
    """Reject HDR and Dolby Vision while retaining ordinary HEVC streams."""
    if int(_number(metadata.get("id"))) in {125, 126}:
        return True
    for key in ("hdr", "is_hdr", "isHdr", "hdr_type", "hdrType", "dynamic_range", "dynamicRange"):
        if key not in metadata:
            continue
        value = str(metadata.get(key) or "").strip().lower()
        if value not in {"", "0", "false", "none", "null", "sdr", "standard"}:
            return True
    text = _search_text(metadata, url, context)
    return bool(
        re.search(r"(^|[^a-z0-9])hdr(?:10|400|pq|hlg)?([^a-z0-9]|$)", text)
        or "dolby vision" in text
        or "dolby_vision" in text
        or "dolbyvision" in text
    )


def codec_preference(metadata: dict[str, Any], url: str = "", context: str = "") -> int:
    """HEVC/H.265 is preferred, followed by H.264/AVC, then other codecs."""
    codecid = int(_number(metadata.get("codecid") or metadata.get("codec_id")))
    if codecid == 12:
        return 3
    if codecid == 7:
        return 2
    text = _search_text(metadata, url, context)
    if any(token in text for token in ("hev1", "hvc1", "hevc", "h.265", "h265", "bytevc1")):
        return 3
    if any(token in text for token in ("avc1", "avc", "h.264", "h264")):
        return 2
    return 1


def _dimensions(metadata: dict[str, Any], text: str) -> tuple[int, int]:
    width = int(_number(metadata.get("width") or metadata.get("w")))
    height = int(_number(metadata.get("height") or metadata.get("h")))
    dimension = metadata.get("dimension")
    if isinstance(dimension, dict):
        width = max(width, int(_number(dimension.get("width") or dimension.get("w"))))
        height = max(height, int(_number(dimension.get("height") or dimension.get("h"))))
    matches = re.findall(r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)", text)
    dimensions = [
        (int(raw_width), int(raw_height)) for raw_width, raw_height in matches
        if int(raw_width) >= int(raw_height)
    ]
    if dimensions:
        width, height = max(dimensions, key=lambda value: value[0] * value[1])
    labels = {"8k": 4320, "4320p": 4320, "4k": 2160, "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720}
    for label, candidate_height in labels.items():
        if re.search(rf"(^|[^a-z0-9]){re.escape(label)}([^a-z0-9]|$)", text):
            height = max(height, candidate_height)
    quality_id = int(_number(metadata.get("id") or metadata.get("quality") or metadata.get("quality_id")))
    height = max(height, _BILIBILI_HEIGHTS.get(quality_id, 0))
    return width, height


def video_stream_score(metadata: dict[str, Any], url: str = "", context: str = "") -> tuple[int, int, int, float, float]:
    """Highest non-HDR resolution wins; codec preference breaks equal-resolution ties."""
    text = _search_text(metadata, url, context)
    width, height = _dimensions(metadata, text)
    fps = max(
        _number(metadata.get("frame_rate")),
        _number(metadata.get("frameRate")),
        _number(metadata.get("fps")),
    )
    if not fps:
        match = re.search(r"(?:^|[^0-9])(120|60|50|30|25|24)\s*fps", text)
        fps = _number(match.group(1)) if match else 0
    bitrate = max(
        _number(metadata.get("bandwidth")),
        _number(metadata.get("bitrate")),
        _number(metadata.get("bit_rate")),
        _number(metadata.get("data_size")),
        _number(metadata.get("size")),
    )
    return height, width, codec_preference(metadata, url, context), fps, bitrate


def audio_stream_score(metadata: dict[str, Any], url: str = "", context: str = "") -> tuple[int, float, float, float]:
    """Prefer lossless/immersive audio, then sample rate, bit depth and bitrate."""
    text = _search_text(metadata, url, context)
    if any(token in text for token in ("flac", "lossless", "hi-res", "hi_res", "hires")):
        codec_rank = 5
    elif any(token in text for token in ("eac3", "ec-3", "dolby", "atmos")):
        codec_rank = 4
    elif "opus" in text:
        codec_rank = 3
    elif any(token in text for token in ("mp4a", "aac")):
        codec_rank = 2
    else:
        codec_rank = 1
    sample_rate = max(_number(metadata.get("sample_rate")), _number(metadata.get("sampleRate")))
    bit_depth = max(_number(metadata.get("bit_depth")), _number(metadata.get("bitDepth")))
    bitrate = max(
        _number(metadata.get("bandwidth")),
        _number(metadata.get("bitrate")),
        _number(metadata.get("bit_rate")),
        _number(metadata.get("size")),
    )
    return codec_rank, sample_rate, bit_depth, bitrate
