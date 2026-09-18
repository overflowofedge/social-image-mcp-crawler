from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Platform(str, Enum):
    DOUYIN = "douyin"
    XHS = "xhs"
    WEIBO = "weibo"
    BILIBILI = "bilibili"
    X = "x"
    INSTAGRAM = "instagram"
    OTHER = "other"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    platforms: list[Platform] | None = None
    max_results: int = Field(default=20, ge=1, le=200)
    min_width: int = Field(default=0, ge=0, le=20000)
    min_height: int = Field(default=0, ge=0, le=20000)
    safe_mode: bool = True
    use_cache: bool = True
    retrieval_mode: str = Field(default="sources", pattern="^(sources|discovery|hybrid|platform)$")
    media_type: str = Field(default="images", pattern="^(images|videos|all)$")
    image_limit: int | None = Field(default=None, ge=1, le=200)
    video_limit: int | None = Field(default=None, ge=1, le=200)
    per_post_limit: int | None = Field(default=None, ge=1, le=50)


class CreatorSort(str, Enum):
    RECENT = "recent"
    POPULAR = "popular"


class CreatorFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform
    creator_id: str | None = Field(default=None, min_length=1, max_length=500)
    creator_name: str | None = Field(default=None, min_length=1, max_length=200)
    profile_url: str | None = Field(default=None, min_length=1, max_length=2000)
    max_posts: int = Field(default=20, ge=1, le=100)
    max_images: int = Field(default=50, ge=1, le=200)
    max_videos: int | None = Field(default=None, ge=1, le=200)
    per_post_limit: int | None = Field(default=None, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=1000)
    since: datetime | None = None
    until: datetime | None = None
    sort: CreatorSort = CreatorSort.RECENT
    include_video_covers: bool = False
    media_type: str = Field(default="images", pattern="^(images|videos|all)$")
    content_query: str | None = Field(default=None, min_length=1, max_length=500)
    filter_mode: str = Field(default="off", pattern="^(off|optional|required)$")
    quality_mode: str = Field(default="fast", pattern="^(fast|balanced|strict)$")
    safe_mode: bool = True
    download: bool = True
    output_dir: str | None = None
    resume: bool = True
    min_width: int = Field(default=0, ge=0, le=20000)
    min_height: int = Field(default=0, ge=0, le=20000)
    max_concurrency: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_creator_target(self) -> "CreatorFetchRequest":
        # Chinese input is almost always a Douyin display name. Accept it in
        # creator_id for a low-friction MCP call, while keeping creator_name
        # explicit for non-Chinese nicknames.
        if self.platform == Platform.DOUYIN and self.creator_id and not self.creator_name and any("\u4e00" <= char <= "\u9fff" for char in self.creator_id):
            self.creator_name, self.creator_id = self.creator_id, None
        if self.platform == Platform.X and self.creator_id:
            self.creator_id = self.creator_id.strip().lstrip("@")
        if self.platform not in (Platform.DOUYIN, Platform.WEIBO, Platform.BILIBILI, Platform.X, Platform.INSTAGRAM, Platform.XHS):
            raise ValueError("unsupported creator platform")
        if self.creator_name and self.platform not in (Platform.DOUYIN, Platform.WEIBO, Platform.BILIBILI, Platform.X, Platform.INSTAGRAM):
            raise ValueError("creator_name lookup is not supported for this platform; use a profile URL")
        targets = [bool(self.creator_id), bool(self.creator_name), bool(self.profile_url)]
        if sum(targets) != 1:
            raise ValueError("provide exactly one of creator_id, creator_name or profile_url")
        for field in ("since", "until"):
            value = getattr(self, field)
            if value and value.tzinfo is None:
                setattr(self, field, value.replace(tzinfo=timezone.utc))
        if self.since and self.until and self.since > self.until:
            raise ValueError("since must be earlier than or equal to until")
        if self.filter_mode != "off" and not self.content_query:
            raise ValueError("content_query is required when filter_mode is optional or required")
        if self.content_query and self.filter_mode == "off":
            self.filter_mode = "optional"
        if self.filter_mode == "required" and self.quality_mode == "fast":
            self.quality_mode = "strict"
        return self


class CreatorIdentity(BaseModel):
    platform: Platform
    requested_id: str
    canonical_id: str = Field(min_length=1)
    name: str = ""
    profile_url: str | None = None
    source: str
    matched_by: str


class ImageCandidate(BaseModel):
    id: str
    platform: Platform
    image_url: str
    media_type: str = Field(default="image", pattern="^(image|video)$")
    thumbnail_url: str | None = None
    permalink: str | None = None
    title: str = ""
    description: str = ""
    author: str = ""
    alt_text: str = ""
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    published_at: str | None = None
    creator_id: str | None = None
    creator_name: str = ""
    post_id: str | None = None
    media_index: int | None = Field(default=None, ge=1)
    engagement_score: float = Field(default=0.0, ge=0.0)
    score: float = 0.0
    matched_terms: list[str] = Field(default_factory=list)
    source_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def stable_key(self) -> str:
        return f"{self.platform.value}:{self.id}:{self.image_url}"


class DownloadRequest(BaseModel):
    items: list[ImageCandidate] = Field(min_length=1, max_length=400)
    output_dir: str | None = None
    max_concurrency: int = Field(default=5, ge=1, le=20)
    min_width: int = Field(default=0, ge=0)
    min_height: int = Field(default=0, ge=0)


class DownloadRecord(BaseModel):
    candidate_id: str
    platform: Platform
    image_url: str
    media_type: str = Field(default="image", pattern="^(image|video)$")
    path: str | None = None
    sha256: str | None = None
    perceptual_hash: str | None = None
    width: int | None = None
    height: int | None = None
    content_type: str | None = None
    creator_id: str | None = None
    post_id: str | None = None
    media_index: int | None = None
    status: str
    error: str | None = None
