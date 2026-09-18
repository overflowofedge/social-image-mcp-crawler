from __future__ import annotations

import math
import re
from collections.abc import Iterable

from .intent import Intent, expand_token
from .models import ImageCandidate


def _text(candidate: ImageCandidate) -> str:
    def flatten(value: object) -> list[str]:
        if isinstance(value, dict):
            return [part for key, child in value.items() if key not in {"url", "url_list", "image_url", "image_urls"} for part in flatten(child)]
        if isinstance(value, list):
            return [part for child in value for part in flatten(child)]
        return [str(value)] if isinstance(value, (str, int, float)) else []

    payload_text = " ".join(flatten(candidate.source_payload))
    return " ".join((candidate.title, candidate.description, candidate.author, candidate.alt_text, candidate.permalink or "", payload_text)).lower()


def _quality(candidate: ImageCandidate) -> float:
    if not candidate.width or not candidate.height:
        return 0.15
    pixels = candidate.width * candidate.height
    # Smoothly favor useful source images without letting very large files dominate.
    return min(0.45, math.log10(max(pixels, 1)) / 18)


def score_candidate(candidate: ImageCandidate, intent: Intent) -> tuple[float, list[str]]:
    haystack = _text(candidate)
    matched: list[str] = []
    token_score = 0.0
    for token in intent.tokens:
        variants = expand_token(token)
        if any(re.search(re.escape(variant), haystack) for variant in variants):
            matched.append(token)
            token_score += 1.0 if len(token) > 1 else 0.35
    negative_hit = any(re.search(re.escape(token), haystack) for token in intent.negative_tokens)
    url_text = candidate.image_url.lower()
    low_quality_url = any(term in url_text for term in ("thumb", "thumbnail", "avatar", "small", "lowres", "preview"))
    watermark_hit = any(term in haystack for term in ("水印", "watermark", "logo"))
    overlay_hit = any(term in haystack for term in ("文字", "text overlay", "字幕"))
    if negative_hit or (intent.exclude_watermark and watermark_hit) or (intent.exclude_text_overlay and overlay_hit):
        return -1.0, matched

    exact = 0.0
    source_record = candidate.source_payload.get("record", {})
    post_id = source_record.get("post_id") if isinstance(source_record, dict) else None
    if intent.identifier and (
        candidate.id == intent.identifier
        or str(post_id or "") == intent.identifier
        or intent.identifier in (candidate.permalink or "")
    ):
        exact = 3.0
    orientation = 0.0
    if intent.orientation and candidate.width and candidate.height:
        ratio = candidate.width / max(candidate.height, 1)
        expected = {"landscape": ratio > 1.15, "portrait": ratio < 0.87, "square": 0.87 <= ratio <= 1.15}[intent.orientation]
        orientation = 0.6 if expected else -0.25
    quality_signal = _quality(candidate)
    if intent.quality_preference == "high":
        quality_signal += 0.35 if not low_quality_url else -0.35
    score = exact + min(token_score / max(len(intent.tokens), 1), 1.0) * 2.0 + orientation + quality_signal
    return score, matched


def rank_candidates(candidates: Iterable[ImageCandidate], intent: Intent, max_results: int, min_width: int = 0, min_height: int = 0, require_match: bool = False) -> list[ImageCandidate]:
    ranked: list[ImageCandidate] = []
    for candidate in candidates:
        if (candidate.width and candidate.width < min_width) or (candidate.height and candidate.height < min_height):
            continue
        score, matched = score_candidate(candidate, intent)
        if score < 0:
            continue
        # A keyword result must have at least one textual/metadata hit.  The
        # old quality-only fallback let unrelated large images (score ~0.15)
        # outrank genuinely relevant but smaller posts, especially on Douyin
        # and Weibo where search feeds contain broad recommendations.
        if require_match and intent.is_keyword and not matched:
            continue
        ranked.append(candidate.model_copy(update={"score": round(score, 4), "matched_terms": matched}))
    ranked.sort(key=lambda item: (item.score, (item.width or 0) * (item.height or 0), "?" not in item.image_url), reverse=True)
    unique: list[ImageCandidate] = []
    seen: set[str] = set()
    for item in ranked:
        key = item.image_url.split("?", 1)[0].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= max_results:
            break
    return unique
