from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_path(value: str | None, default: str) -> str:
    path = Path(value or default).expanduser()
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


# Load the project-local .env even when an MCP client launches us from a
# different working directory.  Environment variables already supplied by the
# client remain authoritative because override=False is the default.
load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class Settings:
    cache_path: str = _project_path(os.getenv("SOCIAL_IMAGE_CACHE"), ".cache/social-image-mcp.sqlite3")
    output_dir: str = _project_path(os.getenv("SOCIAL_IMAGE_OUTPUT"), "downloads")
    source_verification_path: str = _project_path(os.getenv("SOURCE_VERIFICATION_PATH"), ".cache/source-verification.json")
    source_verification_ttl_seconds: int = int(os.getenv("SOURCE_VERIFICATION_TTL_SECONDS", "86400"))
    x_bearer_token: str | None = os.getenv("X_BEARER_TOKEN") or None
    instagram_access_token: str | None = os.getenv("INSTAGRAM_ACCESS_TOKEN") or None
    instagram_user_id: str | None = os.getenv("INSTAGRAM_USER_ID") or None
    meta_graph_version: str = os.getenv("META_GRAPH_VERSION", "v26.0")
    weibo_access_token: str | None = os.getenv("WEIBO_ACCESS_TOKEN") or None
    weibo_cookie: str | None = os.getenv("WEIBO_COOKIE") or None
    bilibili_cookie: str | None = os.getenv("BILIBILI_COOKIE") or None
    native_api_timeout_seconds: float = float(os.getenv("NATIVE_API_TIMEOUT_SECONDS", "12"))
    browser_fallback: bool = os.getenv("BROWSER_FALLBACK", "false").lower() in {"1", "true", "yes"}
    browser_headless: bool = os.getenv("BROWSER_HEADLESS", "true").lower() in {"1", "true", "yes"}
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "30000"))
    browser_cdp_url: str | None = os.getenv("BROWSER_CDP_URL") or None
    browser_user_data_dir: str | None = os.getenv("BROWSER_USER_DATA_DIR") or None
    browser_channel: str | None = os.getenv("BROWSER_CHANNEL") or None
    search_timeout_seconds: int = int(os.getenv("SEARCH_TIMEOUT_SECONDS", "55"))
    creator_timeout_seconds: int = int(os.getenv("CREATOR_TIMEOUT_SECONDS", "120"))
    object_filter_timeout_seconds: float = float(os.getenv("OBJECT_FILTER_TIMEOUT_SECONDS", "4"))
    platform_timeout_seconds: int = int(os.getenv("PLATFORM_TIMEOUT_SECONDS", "50"))
    semantic_model: str | None = os.getenv("SEMANTIC_MODEL") or None
    semantic_timeout_seconds: float = float(os.getenv("SEMANTIC_TIMEOUT_SECONDS", "4"))
    vision_model: str | None = os.getenv("VISION_MODEL", "openai/clip-vit-base-patch32") or None
    vision_timeout_seconds: int = int(os.getenv("VISION_TIMEOUT_SECONDS", "8"))
    vision_image_timeout_seconds: int = int(os.getenv("VISION_IMAGE_TIMEOUT_SECONDS", "8"))
    vision_local_only: bool = os.getenv("VISION_LOCAL_ONLY", "true").lower() in {"1", "true", "yes"}
    object_model: str | None = os.getenv("OBJECT_MODEL") or None
    object_label_map: str | None = os.getenv("OBJECT_LABEL_MAP") or None
    object_confidence: float = float(os.getenv("OBJECT_CONFIDENCE", "0.35"))
    media_crawler_command: str | None = os.getenv("MEDIA_CRAWLER_COMMAND") or None
    xhs_downloader_command: str | None = os.getenv("XHS_DOWNLOADER_COMMAND") or None
    douyin_source_command: str | None = os.getenv("DOUYIN_SOURCE_COMMAND") or None
    bilibili_source_command: str | None = os.getenv("BILIBILI_SOURCE_COMMAND") or None
    gallery_dl_binary: str = os.getenv("GALLERY_DL_BINARY", "gallery-dl")
    gallery_dl_config: str | None = os.getenv("GALLERY_DL_CONFIG") or None
    gallery_dl_cookies_from_browser: str | None = os.getenv("GALLERY_DL_COOKIES_FROM_BROWSER") or None
    gallery_dl_cookies_file: str | None = _project_path(os.getenv("GALLERY_DL_COOKIES_FILE"), ".cache/gallery-dl-cookies.txt") if os.getenv("GALLERY_DL_COOKIES_FILE") else None
    source_timeout_seconds: int = int(os.getenv("SOURCE_TIMEOUT_SECONDS", "45"))
    source_failure_cooldown_seconds: int = int(os.getenv("SOURCE_FAILURE_COOLDOWN_SECONDS", "120"))
    douyin_source_timeout_seconds: int = int(os.getenv("DOUYIN_SOURCE_TIMEOUT_SECONDS", "45"))
    douyin_media_crawler_fallback: bool = os.getenv("DOUYIN_MEDIA_CRAWLER_FALLBACK", "false").lower() in {"1", "true", "yes"}
    douyin_search_endpoint: str | None = os.getenv("DOUYIN_SEARCH_ENDPOINT") or None
    douyin_item_endpoint: str | None = os.getenv("DOUYIN_ITEM_ENDPOINT") or None
    douyin_cookie: str | None = os.getenv("DOUYIN_COOKIE") or None
    douyin_skill_path: str = os.getenv("DOUYIN_SKILL_PATH", "~/.agents/skills/douyin-spider")
    douyin_native_skill: bool = os.getenv("DOUYIN_NATIVE_SKILL", "true").lower() in {"1", "true", "yes"}
    xhs_search_endpoint: str | None = os.getenv("XHS_SEARCH_ENDPOINT") or None
    xhs_item_endpoint: str | None = os.getenv("XHS_ITEM_ENDPOINT") or None
    xhs_cookies: str | None = os.getenv("XHS_COOKIES") or None
    weibo_search_endpoint: str | None = os.getenv("WEIBO_SEARCH_ENDPOINT") or None
    weibo_item_endpoint: str | None = os.getenv("WEIBO_ITEM_ENDPOINT") or None

    def ensure_output_dir(self, override: str | None = None) -> Path:
        path = Path(override or self.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
