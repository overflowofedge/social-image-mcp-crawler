from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")
_PLATFORM_PREFIX = re.compile(r"^(douyin|抖音|xhs|小红书|xiaohongshu|weibo|微博|bilibili|bili|b站|x|twitter|instagram|ins)(-user|-name|-nickname|昵称|博主)?[:：]([\w.\-\u4e00-\u9fff]+)$", re.I)
_URL_ID_PATTERNS = {
    "douyin": re.compile(r"/video/(\d+)|/note/(\w+)", re.I),
    "xhs": re.compile(r"/explore/([\w-]+)|/discovery/item/([\w-]+)", re.I),
    "weibo": re.compile(r"/\w+/([A-Za-z0-9]+)", re.I),
    "x": re.compile(r"/(?:status|statuses)/(\d+)", re.I),
    "instagram": re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.I),
    "bilibili": re.compile(r"/(?:video|bangumi/play)/((?:BV|bv)[A-Za-z0-9]+|av\d+)", re.I),
}

_ALIASES = {
    "室内": {"interior", "indoors", "room"},
    "咖啡店": {"cafe", "coffee", "coffee shop"},
    "横图": {"landscape", "horizontal", "wide"},
    "竖图": {"portrait", "vertical", "tall"},
    "方图": {"square"},
    "高清": {"hd", "high resolution", "4k"},
    "人像": {"portrait", "people", "person"},
    "风景": {"landscape", "scenery", "nature"},
    "产品": {"product", "product photo"},
    "穿搭": {"outfit", "fashion", "clothing", "street style"},
    "生活": {"lifestyle", "daily life", "person"},
    "场景": {"scene", "setting", "background"},
    "纯场景": {"empty scene", "background only", "setting"},
    "建筑": {"architecture", "building", "exterior"},
    "街景": {"street view", "street", "city scene"},
    "食物": {"food", "dish", "meal"},
    "饮品": {"drink", "beverage", "coffee"},
}

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def requested_media_limit(query: str, default: int = 20, maximum: int = 100) -> int:
    """Honor explicit quantity words in a natural-language request.

    Examples: ``最多3张`` and ``只要两张``.  The value never increases the
    caller's configured limit for numeric requests; ``全部/完整图集`` opts
    into the API maximum.
    """
    text = _norm(query)
    match = re.search(r"(?:最多|不超过|只要|仅要|取前|下载)\s*(\d+|[一二两三四五六七八九十])\s*(?:张|个|条|份|幅|张图)?", text)
    if match:
        raw = match.group(1)
        value = int(raw) if raw.isdigit() else _CN_DIGITS.get(raw, default)
        return max(1, min(default, value))
    if re.search(r"(?:全部|所有|完整|整组)\s*(?:图集|作品|媒体|图片|视频)?", text):
        return maximum
    return default


@dataclass(frozen=True)
class Intent:
    raw: str
    normalized: str
    tokens: tuple[str, ...]
    negative_tokens: tuple[str, ...]
    identifier: str | None = None
    identifier_platform: str | None = None
    identifier_scope: str | None = None
    url: str | None = None
    orientation: str | None = None
    quality_preference: str = "balanced"
    exclude_watermark: bool = False
    exclude_text_overlay: bool = False

    @property
    def is_identifier(self) -> bool:
        return self.identifier is not None or self.url is not None

    @property
    def is_keyword(self) -> bool:
        return not self.is_identifier


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


def _tokens(text: str) -> list[str]:
    normalized = _norm(text)
    tokens = _TOKEN_RE.findall(normalized)
    # Keep useful multi-word English aliases alongside single tokens.
    return tokens + [value for value in _ALIASES if value in normalized]


def parse_intent(query: str) -> Intent:
    raw = query.strip()
    normalized = _norm(raw)
    url = raw if re.match(r"https?://", raw, re.I) else None
    identifier = None
    identifier_platform = None
    identifier_scope = None

    # Match the original input so case-sensitive Instagram shortcodes and
    # Weibo bid values survive parsing unchanged. Only the platform alias is
    # normalized.
    match = _PLATFORM_PREFIX.match(raw)
    if match:
        platform_name = match.group(1).lower()
        identifier_platform = {"抖音": "douyin", "小红书": "xhs", "微博": "weibo", "xiaohongshu": "xhs", "twitter": "x", "ins": "instagram", "bili": "bilibili", "b站": "bilibili"}.get(platform_name, platform_name)
        suffix = (match.group(2) or "").lower()
        identifier_scope = "creator_name" if suffix in {"-name", "-nickname", "昵称", "博主"} else ("creator" if suffix else "post")
        identifier = match.group(3)
    elif re.fullmatch(r"@[A-Za-z0-9_.-]+", raw):
        identifier = raw[1:]
        identifier_platform = "x"
        identifier_scope = "creator"
    elif re.fullmatch(r"from:[A-Za-z0-9_.-]+", raw, re.I):
        identifier = raw.split(":", 1)[1]
        identifier_platform = "x"
        identifier_scope = "creator"
    elif re.fullmatch(r"\d{6,}|[A-Za-z0-9_-]{10,}", raw):
        identifier = raw
        identifier_scope = "post"
    elif url:
        host = (urlparse(url).hostname or "").lower()
        if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            identifier_platform = "x"
        elif host in {"instagram.com", "www.instagram.com"}:
            identifier_platform = "instagram"
        elif host in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}:
            identifier_platform = "xhs"
        elif host in {"douyin.com", "www.douyin.com", "v.douyin.com"}:
            identifier_platform = "douyin"
        elif host in {"weibo.com", "www.weibo.com", "m.weibo.cn"}:
            identifier_platform = "weibo"
        elif host in {"bilibili.com", "www.bilibili.com", "b23.tv", "www.b23.tv", "space.bilibili.com"}:
            identifier_platform = "bilibili"
        else:
            identifier_platform = "other"
        if identifier_platform and identifier_platform in _URL_ID_PATTERNS:
            found = _URL_ID_PATTERNS[identifier_platform].search(urlparse(url).path)
            if found:
                identifier = next(group for group in found.groups() if group)
                identifier_scope = "post"
        parsed_path = urlparse(url).path
        if identifier_platform == "douyin":
            creator_match = re.search(r"/(?:share/)?user/([^/?]+)", parsed_path, re.I)
            if creator_match and not re.search(r"/(?:video|note)/", parsed_path, re.I):
                identifier = creator_match.group(1)
                identifier_scope = "creator"
        elif identifier_platform == "weibo":
            creator_match = re.fullmatch(r"/(?:u/|profile/)?(\d+)/?", parsed_path, re.I)
            if creator_match:
                identifier = creator_match.group(1)
                identifier_scope = "creator"
        elif identifier_platform == "bilibili":
            creator_match = re.fullmatch(r"/(?:u/)?(\d+)/?", parsed_path, re.I) if host.startswith("space.") else None
            if creator_match:
                identifier = creator_match.group(1)
                identifier_scope = "creator"
        if identifier_platform == "douyin":
            modal = parse_qs(urlparse(url).query).get("modal_id", [""])[0]
            if modal.isdigit():
                identifier, identifier_scope = modal, "post"
        elif identifier_platform == "x":
            profile = re.fullmatch(r"/([A-Za-z0-9_.-]+)/?", parsed_path)
            if profile and profile.group(1).lower() not in {"home", "explore", "search", "settings", "i"}:
                identifier, identifier_scope = profile.group(1), "creator"
        elif identifier_platform == "instagram":
            profile = re.fullmatch(r"/([A-Za-z0-9_.-]+)/?", parsed_path)
            if profile and profile.group(1).lower() not in {"explore", "accounts", "direct"}:
                identifier, identifier_scope = profile.group(1), "creator"
        elif identifier_platform == "xhs":
            profile = re.fullmatch(r"/user/profile/([A-Za-z0-9_-]+)/?", parsed_path, re.I)
            if profile:
                identifier, identifier_scope = profile.group(1), "creator"

    negative: list[str] = []
    # Capture comma/顿号 separated exclusion lists, e.g. "排除风景、建筑、纯场景图".
    exclusion_matches = re.findall(r"(?:不要|去掉|排除)\s*([^，,。；;]+)", normalized)
    for group in exclusion_matches:
        parts = re.split(r"[、，,\s和及]+", group)
        for part in parts:
            part = part.strip("图照片图片")
            if part:
                negative.extend(_tokens(part))
    negative.extend(_tokens(" ".join(re.findall(r"-(\w+)", normalized))))
    positive_text = re.sub(r"(?:不要|去掉|排除)\s*[^，,。；;]+", " ", normalized)
    positive_text = re.sub(r"-\w+", " ", positive_text)
    tokens = _tokens(positive_text)
    orientation = None
    if any(token in tokens for token in ("横图", "landscape", "horizontal", "wide")):
        orientation = "landscape"
    elif any(token in tokens for token in ("竖图", "portrait", "vertical", "tall")):
        orientation = "portrait"
    elif any(token in tokens for token in ("方图", "square")):
        orientation = "square"

    quality_preference = "high" if any(term in normalized for term in ("高清", "高分辨率", "原图", "无水印", "不要水印", "去水印", "4k", "hd", "high resolution")) else "balanced"
    exclude_watermark = "水印" in negative or any(term in normalized for term in ("无水印", "不要水印", "去水印"))
    exclude_text_overlay = "文字" in negative or any(term in normalized for term in ("无文字", "不要文字", "无字"))

    return Intent(raw, normalized, tuple(dict.fromkeys(tokens)), tuple(dict.fromkeys(negative)), identifier, identifier_platform, identifier_scope, url, orientation, quality_preference, exclude_watermark, exclude_text_overlay)


def expand_token(token: str) -> set[str]:
    expanded = {token}
    for key, aliases in _ALIASES.items():
        if token == key or token in aliases:
            expanded.update({key, *aliases})
    return expanded


def visual_prompt(intent: Intent) -> str:
    """Build a bilingual CLIP prompt from the parsed visual brief."""
    english: list[str] = []
    for token in intent.tokens:
        aliases = sorted((alias for alias in expand_token(token) if alias.isascii()), key=len, reverse=True)
        if aliases:
            english.append(aliases[0])
    details: list[str] = []
    if intent.orientation:
        details.append(intent.orientation)
    if intent.quality_preference == "high":
        details.append("high resolution original photo")
    if intent.exclude_watermark:
        details.append("without watermark")
    if intent.exclude_text_overlay:
        details.append("without text overlay")
    subject = " ".join(dict.fromkeys(english)) or intent.raw
    return "A real photograph of " + subject + (", " + ", ".join(details) if details else "")


def visual_contrast_prompts(intent: Intent) -> tuple[list[str], list[str]]:
    """Build positive and mutually exclusive visual concepts for CLIP."""
    positive = [visual_prompt(intent)]
    normalized = intent.normalized
    token_variants = {variant for token in intent.tokens for variant in expand_token(token)}

    if "室内" in normalized or token_variants.intersection({"interior", "indoors", "room"}):
        positive.append(
            visual_prompt(intent).replace(
                "A real photograph of ",
                "A wide interior photograph showing the complete space, furniture, walls and architecture of ",
                1,
            )
        )
        negative = [
            "A close-up photograph of food, a drink, a product, or a small object",
            "An outdoor scene, exterior architecture, or portrait of a person",
        ]
    elif "人像" in normalized or token_variants.intersection({"portrait", "people", "person"}):
        negative = [
            "An empty room interior without a person",
            "A product, food, drink, or outdoor landscape without a person",
        ]
    elif token_variants.intersection({"穿搭", "outfit", "fashion", "clothing", "street style", "生活", "lifestyle", "daily life"}):
        positive.append("A lifestyle photograph featuring a person as the main subject, wearing an outfit or documenting daily life")
        negative = [
            "An outdoor landscape, mountain, sea, sky, or scenic view without a person",
            "An empty scene, building, street, or architecture photograph without a person as the main subject",
        ]
    elif "产品" in normalized or token_variants.intersection({"product", "product photo"}):
        negative = [
            "A wide room interior or outdoor landscape",
            "A portrait photograph where the person is the main subject",
        ]
    elif "风景" in normalized or token_variants.intersection({"landscape", "scenery", "nature"}):
        negative = [
            "A close-up product, food, drink, or small object photograph",
            "An indoor room or portrait photograph",
        ]
    else:
        negative = []
    # User-supplied exclusions are stronger than the generic branch defaults.
    # Keep them as visual concepts so "穿搭但不要风景/纯场景" works even when
    # the positive brief does not contain one of the built-in categories.
    negative_terms = set(intent.negative_tokens)
    exclusion_prompts = []
    if negative_terms.intersection({"风景", "landscape", "scenery", "nature"}):
        exclusion_prompts.append("An outdoor landscape, mountain, sea, sky, or scenic view without the requested subject")
    if negative_terms.intersection({"场景", "纯场景", "建筑", "街景", "architecture", "street"}):
        exclusion_prompts.append("An empty scene, building, street, or architecture photograph without a person as the main subject")
    if negative_terms.intersection({"人物", "人像", "person", "people"}):
        exclusion_prompts.append("A photograph dominated by a person or portrait")
    if negative_terms.intersection({"食物", "饮品", "food", "drink"}):
        exclusion_prompts.append("A close-up photograph of food or a drink")
    negative.extend(exclusion_prompts)
    return list(dict.fromkeys(positive)), negative
