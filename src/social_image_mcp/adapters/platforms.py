from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from ..config import Settings
from ..intent import Intent
from ..models import ImageCandidate, Platform
from ..bilibili import BilibiliApi, BilibiliError
from ..weibo import WeiboApi, WeiboError
from .base import AdapterError, AdapterStatus, AdapterUnavailable, PlatformAdapter


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            return value
    return None


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "notes", "statuses", "medias", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _image_urls(record: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("image_url", "imageUrl", "url", "display_url", "displayUrl", "original_url", "originalUrl", "cover", "cover_url", "thumbnail_url"):
        value = record.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.append(value)
    for key in ("images", "image_urls", "imageUrls", "media", "resources"):
        values = record.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    urls.append(value)
                elif isinstance(value, dict):
                    urls.extend(_image_urls(value))
    return list(dict.fromkeys(urls))


def _image_metadata(value: Any, image_url: str) -> dict[str, Any]:
    if isinstance(value, dict):
        if any(value.get(key) == image_url for key in ("url", "image_url", "imageUrl", "display_url", "displayUrl", "original_url", "originalUrl")):
            return value
        for child in value.values():
            found = _image_metadata(child, image_url)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _image_metadata(child, image_url)
            if found:
                return found
    return {}


def _candidate(platform: Platform, record: dict[str, Any], image_url: str, index: int, image_meta: dict[str, Any] | None = None) -> ImageCandidate:
    image_meta = image_meta or {}
    item_id = str(_first(record, "id", "item_id", "itemId", "note_id", "noteId", "media_id", "mediaId", "tweet_id", "tweetId") or index)
    return ImageCandidate(
        id=item_id,
        platform=platform,
        image_url=image_url,
        thumbnail_url=_first(record, "thumbnail_url", "thumbnailUrl", "cover", "cover_url"),
        permalink=_first(record, "permalink", "url", "link", "web_url"),
        title=str(_first(record, "title", "text", "caption", "desc", "description") or ""),
        description=str(_first(record, "description", "desc", "text", "caption") or ""),
        author=str(_first(record, "author", "author_name", "username", "user_name", "user") or ""),
        alt_text=str(_first(record, "alt_text", "alt", "accessibility_alt_text") or ""),
        width=_first(image_meta, "width", "image_width") or _first(record, "width", "image_width"),
        height=_first(image_meta, "height", "image_height") or _first(record, "height", "image_height"),
        published_at=_first(record, "published_at", "created_at", "timestamp"),
        source_payload=record,
    )


class ConfiguredJsonAdapter(PlatformAdapter):
    def __init__(self, client: httpx.AsyncClient, platform: Platform, search_endpoint: str | None, item_endpoint: str | None, auth: dict[str, str] | None = None) -> None:
        super().__init__(client)
        self.platform = platform
        self.search_endpoint = search_endpoint
        self.item_endpoint = item_endpoint
        self.auth = auth or {}

    @property
    def status(self) -> AdapterStatus:
        configured = bool(self.search_endpoint or self.item_endpoint)
        mode = "json-gateway" if configured else "not-configured"
        detail = "Configured JSON gateway" if configured else "Set *_SEARCH_ENDPOINT or *_ITEM_ENDPOINT"
        return AdapterStatus(self.platform, configured, mode, detail)

    async def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        try:
            response = await self.client.get(endpoint, params=params, headers=self.auth)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(f"{self.platform.value} gateway request failed: {exc}") from exc

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        endpoint = self.item_endpoint if intent.identifier and self.item_endpoint else self.search_endpoint
        if not endpoint:
            raise AdapterUnavailable(self.status.detail)
        payload = await self._get(endpoint, {"query": intent.raw, "item_id": intent.identifier or "", "limit": limit, "safe_mode": str(safe_mode).lower()})
        candidates: list[ImageCandidate] = []
        for index, record in enumerate(_records(payload)):
            for image_index, url in enumerate(_image_urls(record)):
                candidates.append(_candidate(self.platform, record, url, index * 100 + image_index, _image_metadata(record, url)))
        return candidates


class DouyinSkillAdapter(PlatformAdapter):
    """Optional bridge to the locally installed douyin-spider skill.

    It is intentionally opt-in through a cookie and only calls the skill's
    existing read/search methods; downloads still go through our validator.
    """

    platform = Platform.DOUYIN

    def __init__(self, client: httpx.AsyncClient, cookie: str, skill_path: str) -> None:
        super().__init__(client)
        self.cookie = cookie
        self.skill_path = str(Path(skill_path).expanduser())
        self._load_error: str | None = None
        try:
            if self.skill_path not in sys.path:
                sys.path.insert(0, self.skill_path)
            self._api_module = importlib.import_module("dy_apis.douyin_api")
            self._auth_module = importlib.import_module("builder.auth")
        except Exception as exc:  # Optional dependency; JSON gateway remains available.
            self._api_module = None
            self._auth_module = None
            self._load_error = str(exc)

    @property
    def status(self) -> AdapterStatus:
        if self._api_module and self._auth_module:
            return AdapterStatus(self.platform, True, "native-skill", "Using local douyin-spider skill")
        return AdapterStatus(self.platform, False, "not-configured", self._load_error or "douyin-spider skill could not be loaded")

    @staticmethod
    def _url_list(value: Any) -> list[str]:
        if isinstance(value, dict):
            value = value.get("url_list", [])
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item.startswith(("http://", "https://"))]
        return []

    def _normalize(self, record: dict[str, Any], index: int) -> list[ImageCandidate]:
        record = record.get("aweme_info") or record.get("aweme_detail") or record
        item_id = str(record.get("aweme_id") or record.get("id") or index)
        title = str(record.get("desc") or record.get("title") or "")
        author_data = record.get("author") or {}
        author = str(author_data.get("nickname") or author_data.get("unique_id") or "") if isinstance(author_data, dict) else str(author_data)
        permalink = f"https://www.douyin.com/video/{item_id}"
        urls: list[str] = []
        for image in record.get("images") or []:
            urls.extend(self._url_list(image))
        if not urls:
            video = record.get("video") or {}
            urls.extend(self._url_list(video.get("cover")))
        return [ImageCandidate(id=item_id, platform=self.platform, image_url=url, permalink=permalink, title=title, description=title, author=author, published_at=str(record.get("create_time") or "") or None, source_payload=record) for url in dict.fromkeys(urls)]

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        if not self._api_module or not self._auth_module:
            raise AdapterUnavailable(self.status.detail)

        def call() -> list[dict[str, Any]]:
            auth = self._auth_module.DouyinAuth()
            auth.perepare_auth(self.cookie)
            api = self._api_module.DouyinAPI
            if intent.identifier:
                payload = api.get_work_info(auth, f"https://www.douyin.com/video/{intent.identifier}")
                return [payload.get("aweme_detail", payload)]
            return api.search_some_general_work(auth, intent.raw, limit, "0", "0", content_type="2" if safe_mode else "")

        try:
            rows = await asyncio.to_thread(call)
        except Exception as exc:
            raise AdapterError(f"douyin skill request failed: {exc}") from exc
        result: list[ImageCandidate] = []
        for index, row in enumerate(rows or []):
            if isinstance(row, dict):
                result.extend(self._normalize(row, index))
        return result


class BrowserSearchAdapter(PlatformAdapter):
    """Public-page fallback for platforms without a stable content-search API."""

    def __init__(self, client: httpx.AsyncClient, platform: Platform, search_url_template: str, cookies: str | None = None, headless: bool = True, timeout_ms: int = 30000, cdp_url: str | None = None, user_data_dir: str | None = None, channel: str | None = None) -> None:
        super().__init__(client)
        self.platform = platform
        self.search_url_template = search_url_template
        self.cookies = cookies or ""
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.cdp_url = cdp_url
        self.user_data_dir = str(Path(user_data_dir).expanduser()) if user_data_dir else None
        self.channel = channel
        try:
            import importlib
            importlib.import_module("playwright.async_api")
            self._playwright_error = None
        except Exception as exc:
            self._playwright_error = str(exc)

    @property
    def status(self) -> AdapterStatus:
        if self._playwright_error:
            return AdapterStatus(self.platform, False, "not-configured", f"Install playwright for browser fallback: {self._playwright_error}")
        if self.cdp_url:
            detail = f"Browser fallback uses an existing CDP session at {self.cdp_url}"
        elif self.user_data_dir:
            detail = f"Browser fallback uses the configured {self.channel or 'Chromium'} profile"
        elif self.cookies:
            detail = "Browser fallback uses explicitly supplied cookies"
        else:
            detail = "Public-page browser fallback enabled without an authenticated session"
        return AdapterStatus(self.platform, True, "browser-fallback", detail)

    def _cookies_for(self, domain: str) -> list[dict[str, str]]:
        cookies: list[dict[str, str]] = []
        for part in self.cookies.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if name:
                cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
        return cookies

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        if self._playwright_error:
            raise AdapterUnavailable(self.status.detail)
        from playwright.async_api import async_playwright

        url = self.search_url_template.format(query=quote(intent.raw), item_id=quote(intent.identifier or ""))
        domain = url.split("/", 3)[2]
        blocked_terms = {"nsfw", "porn", "nude", "裸", "色情"}
        browser = None
        context = None
        page = None
        owns_browser = False
        owns_context = False
        try:
            async with async_playwright() as playwright:
                try:
                    if self.cdp_url:
                        browser = await playwright.chromium.connect_over_cdp(self.cdp_url)
                        if browser.contexts:
                            context = browser.contexts[0]
                        else:
                            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, user_agent="social-image-mcp/0.1")
                            owns_context = True
                    elif self.user_data_dir:
                        launch_options = {
                            "headless": self.headless,
                            "viewport": {"width": 1440, "height": 1000},
                            "user_agent": "social-image-mcp/0.1",
                        }
                        if self.channel:
                            launch_options["channel"] = self.channel
                        context = await playwright.chromium.launch_persistent_context(self.user_data_dir, **launch_options)
                        owns_context = True
                    else:
                        launch_options = {"headless": self.headless}
                        if self.channel:
                            launch_options["channel"] = self.channel
                        browser = await playwright.chromium.launch(**launch_options)
                        owns_browser = True
                        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, user_agent="social-image-mcp/0.1")
                        owns_context = True
                    cookies = self._cookies_for(domain)
                    if cookies and not self.cdp_url and not self.user_data_dir:
                        await context.add_cookies(cookies)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    # Give the page a short settle window for lazy-loaded
                    # thumbnails without imposing a fixed 1.2s penalty on
                    # every browser-fallback request.
                    await page.wait_for_timeout(max(100, int(os.getenv("BROWSER_POLL_MS", "500"))))
                    rows = await page.eval_on_selector_all("img", """imgs => imgs.map((img, index) => {
                        const anchor = img.closest('a');
                        const parent = img.parentElement;
                        return {
                          index,
                          src: img.currentSrc || img.src || '',
                          alt: img.alt || '',
                          width: img.naturalWidth || img.width || 0,
                          height: img.naturalHeight || img.height || 0,
                          href: anchor ? anchor.href : '',
                          text: ((parent && parent.innerText) || '').slice(0, 500)
                        };
                    })""")
                finally:
                    # Never close a user's CDP context/browser. Pages created
                    # here are ours; persistent/anonymous contexts are ours too.
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                    if owns_context and context is not None:
                        try:
                            await context.close()
                        except Exception:
                            pass
                    if owns_browser and browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass
        except Exception as exc:
            raise AdapterError(f"{self.platform.value} browser search failed: {exc}") from exc

        result: list[ImageCandidate] = []
        for index, row in enumerate(rows or []):
            image_url = row.get("src", "")
            text = f"{row.get('alt', '')} {row.get('text', '')}".strip()
            if not image_url.startswith(("http://", "https://")) or any(term in text.lower() for term in blocked_terms):
                continue
            width = int(row.get("width") or 0) or None
            height = int(row.get("height") or 0) or None
            if width and height and width < 240 and height < 240:
                continue
            item_id = row.get("href", "").rstrip("/").split("/")[-1] or str(index)
            result.append(ImageCandidate(id=item_id, platform=self.platform, image_url=image_url, permalink=row.get("href") or None, title=text, description=text, width=width, height=height, source_payload=row))
            if len(result) >= limit * 3:
                break
        return result


class XAdapter(PlatformAdapter):
    platform = Platform.X

    def __init__(self, client: httpx.AsyncClient, token: str | None) -> None:
        super().__init__(client)
        self.token = token

    @property
    def status(self) -> AdapterStatus:
        detail = "X_BEARER_TOKEN is required; video downloads use media.variants from API v2"
        return AdapterStatus(self.platform, bool(self.token), "official-api" if self.token else "not-configured", detail)

    @staticmethod
    def _original_image_url(url: str) -> str:
        """Ask pbs.twimg.com for the original image instead of a thumbnail."""
        if "pbs.twimg.com" not in url:
            return url
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["name"] = "orig"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    @staticmethod
    def _best_video_variant(media: dict[str, Any]) -> str | None:
        variants = media.get("variants") or []
        usable = [
            variant for variant in variants
            if isinstance(variant, dict)
            and str(variant.get("content_type", "")).lower() in {"video/mp4", "video/webm"}
            and str(variant.get("url", "")).startswith(("http://", "https://"))
        ]
        if not usable:
            return None
        # Prefer the highest bitrate; GIFs generally expose only one variant.
        return str(max(usable, key=lambda variant: float(variant.get("bit_rate") or 0)).get("url"))

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        if not self.token:
            raise AdapterUnavailable(self.status.detail)
        headers = {"Authorization": f"Bearer {self.token}"}
        identifier = intent.identifier if intent.identifier and intent.identifier_platform in (None, "x") else None
        if identifier:
            url = f"https://api.x.com/2/tweets/{identifier}"
            params = {"expansions": "attachments.media_keys,author_id", "post.fields": "created_at,text,author_id,possibly_sensitive", "media.fields": "url,preview_image_url,variants,width,height,type,alt_text"}
            response = await self.client.get(url, params=params, headers=headers)
        else:
            query = intent.raw.replace("-", " ")
            if safe_mode:
                query += " -is:retweet"
            # `has:images` can exclude video-only posts.  `has:media` lets the
            # API return photos, GIFs and videos; each item is typed below and
            # the downloader chooses the corresponding file validation path.
            query += " has:media"
            params = {"query": query, "max_results": min(max(limit, 10), 100), "expansions": "attachments.media_keys,author_id", "post.fields": "created_at,text,author_id,possibly_sensitive", "media.fields": "url,preview_image_url,variants,width,height,type,alt_text"}
            response = await self.client.get("https://api.x.com/2/tweets/search/recent", params=params, headers=headers)
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(f"x api request failed: {exc}") from exc
        media_by_key = {media.get("media_key"): media for media in payload.get("includes", {}).get("media", [])}
        result: list[ImageCandidate] = []
        rows = payload.get("data", []) or []
        if isinstance(rows, dict):
            rows = [rows]
        for tweet in rows:
            if safe_mode and tweet.get("possibly_sensitive"):
                continue
            for index, media_key in enumerate(tweet.get("attachments", {}).get("media_keys", [])):
                media = media_by_key.get(media_key, {})
                media_type = "image"
                if media.get("type") == "photo":
                    image_url = self._original_image_url(str(media.get("url") or media.get("preview_image_url") or ""))
                elif media.get("type") in ("animated_gif", "video"):
                    image_url = self._best_video_variant(media)
                    media_type = "video"
                else:
                    image_url = None
                if not image_url:
                    continue
                result.append(ImageCandidate(id=str(tweet.get("id")), platform=self.platform, image_url=image_url, media_type=media_type, thumbnail_url=media.get("preview_image_url"), permalink=f"https://x.com/i/status/{tweet.get('id')}", title=tweet.get("text", ""), description=tweet.get("text", ""), width=media.get("width"), height=media.get("height"), alt_text=media.get("alt_text", ""), published_at=tweet.get("created_at"), source_payload={"tweet": tweet, "media": media, "media_index": index}))
        return result


class InstagramAdapter(PlatformAdapter):
    platform = Platform.INSTAGRAM

    def __init__(self, client: httpx.AsyncClient, token: str | None, user_id: str | None, graph_version: str = "v26.0") -> None:
        super().__init__(client)
        self.token = token
        self.user_id = user_id
        self.graph_version = graph_version

    @property
    def status(self) -> AdapterStatus:
        configured = bool(self.token)
        detail = "INSTAGRAM_ACCESS_TOKEN is required; hashtag search also needs INSTAGRAM_USER_ID"
        return AdapterStatus(self.platform, configured, "official-graph-api" if configured else "not-configured", detail)

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "access_token": self.token}
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(f"instagram api request failed: {exc}") from exc

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        if not self.token:
            raise AdapterUnavailable(self.status.detail)
        if intent.url and intent.identifier_platform == "instagram":
            payload = await self._get(f"https://graph.facebook.com/{self.graph_version}/instagram_oembed", {"url": intent.url, "omitscript": "true"})
            thumbnail = payload.get("thumbnail_url")
            if not thumbnail:
                return []
            return [ImageCandidate(id=str(payload.get("media_id") or intent.identifier or intent.url), platform=self.platform, image_url=thumbnail, thumbnail_url=thumbnail, permalink=intent.url, title=payload.get("title", "") or "", description=payload.get("title", "") or "", author=payload.get("author_name", "") or "", width=payload.get("thumbnail_width"), height=payload.get("thumbnail_height"), source_payload=payload)]
        if intent.identifier:
            payload = await self._get(f"https://graph.facebook.com/{self.graph_version}/{intent.identifier}", {"fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,username"})
            rows = [payload]
        else:
            if not self.user_id:
                raise AdapterUnavailable(self.status.detail)
            tag = intent.raw.lstrip("#").split()[0]
            tag_payload = await self._get(f"https://graph.facebook.com/{self.graph_version}/ig_hashtag_search", {"user_id": self.user_id, "q": tag})
            tag_id = (tag_payload.get("data") or [{}])[0].get("id")
            if not tag_id:
                return []
            media = await self._get(f"https://graph.facebook.com/{self.graph_version}/{tag_id}/recent_media", {"user_id": self.user_id, "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,username", "limit": min(limit, 50)})
            rows = media.get("data", [])
        result: list[ImageCandidate] = []
        for row in rows:
            url = row.get("media_url") or row.get("thumbnail_url")
            if not url:
                continue
            result.append(ImageCandidate(id=str(row.get("id")), platform=self.platform, image_url=url, thumbnail_url=row.get("thumbnail_url"), permalink=row.get("permalink"), title=row.get("caption", "") or "", description=row.get("caption", "") or "", author=row.get("username", "") or "", published_at=row.get("timestamp"), source_payload=row))
        return result


class BilibiliAdapter(PlatformAdapter):
    """Public Bilibili API adapter; no browser or credential is required."""

    platform = Platform.BILIBILI

    def __init__(self, client: httpx.AsyncClient, cookie: str | None = None, timeout_seconds: float = 12) -> None:
        super().__init__(client)
        self.api = BilibiliApi(client, cookie, timeout_seconds)

    @property
    def status(self) -> AdapterStatus:
        return AdapterStatus(self.platform, True, "public-api", "Public Bilibili API; login is optional and may improve rate limits")

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        try:
            return await self.api.search(intent, limit, safe_mode)
        except BilibiliError as exc:
            raise AdapterError(str(exc)) from exc

    async def inspect(self, item_id: str) -> list[ImageCandidate]:
        return await self.search(Intent(raw=item_id, normalized=item_id.lower(), tokens=(item_id.lower(),), negative_tokens=(), identifier=item_id, identifier_platform="bilibili"), 20, True)


class WeiboAdapter(PlatformAdapter):
    platform = Platform.WEIBO

    def __init__(self, client: httpx.AsyncClient, cookie: str | None = None, timeout_seconds: float = 12) -> None:
        super().__init__(client)
        self.api = WeiboApi(client, cookie, timeout_seconds)

    @property
    def status(self) -> AdapterStatus:
        return AdapterStatus(self.platform, True, "public-api", "Weibo public API; login cookie improves availability")

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        try:
            return await self.api.search(intent, limit, safe_mode)
        except WeiboError as exc:
            raise AdapterError(str(exc)) from exc

    async def inspect(self, item_id: str) -> list[ImageCandidate]:
        return await self.search(Intent(raw=item_id, normalized=item_id.lower(), tokens=(item_id.lower(),), negative_tokens=(), identifier=item_id, identifier_platform="weibo"), 20, True)

def build_adapters(settings: Settings, client: httpx.AsyncClient) -> dict[Platform, PlatformAdapter]:
    from ..webpage import WebPageAdapter
    if settings.douyin_native_skill and settings.douyin_cookie and not settings.douyin_search_endpoint and not settings.douyin_item_endpoint:
        douyin_adapter: PlatformAdapter = DouyinSkillAdapter(client, settings.douyin_cookie, settings.douyin_skill_path)
    else:
        douyin_adapter = ConfiguredJsonAdapter(client, Platform.DOUYIN, settings.douyin_search_endpoint, settings.douyin_item_endpoint, {"Cookie": settings.douyin_cookie} if settings.douyin_cookie else None)
    if settings.xhs_search_endpoint or settings.xhs_item_endpoint:
        xhs_adapter: PlatformAdapter = ConfiguredJsonAdapter(client, Platform.XHS, settings.xhs_search_endpoint, settings.xhs_item_endpoint, {"Cookie": settings.xhs_cookies} if settings.xhs_cookies else None)
    elif settings.browser_fallback:
        xhs_adapter = BrowserSearchAdapter(client, Platform.XHS, "https://www.xiaohongshu.com/search_result?keyword={query}", settings.xhs_cookies, settings.browser_headless, settings.browser_timeout_ms, settings.browser_cdp_url, settings.browser_user_data_dir, settings.browser_channel)
    else:
        xhs_adapter = ConfiguredJsonAdapter(client, Platform.XHS, None, None)
    if settings.weibo_search_endpoint or settings.weibo_item_endpoint:
        weibo_adapter: PlatformAdapter = ConfiguredJsonAdapter(client, Platform.WEIBO, settings.weibo_search_endpoint, settings.weibo_item_endpoint, {"Authorization": f"Bearer {settings.weibo_access_token}", "Cookie": settings.weibo_cookie} if settings.weibo_access_token or settings.weibo_cookie else None)
    elif settings.browser_fallback:
        weibo_adapter = BrowserSearchAdapter(client, Platform.WEIBO, "https://s.weibo.com/weibo?q={query}", settings.weibo_cookie, settings.browser_headless, settings.browser_timeout_ms, settings.browser_cdp_url, settings.browser_user_data_dir, settings.browser_channel)
    else:
        weibo_adapter = ConfiguredJsonAdapter(client, Platform.WEIBO, None, None)
    return {
        Platform.DOUYIN: douyin_adapter,
        Platform.XHS: xhs_adapter,
        Platform.WEIBO: weibo_adapter if (settings.weibo_search_endpoint or settings.weibo_item_endpoint or settings.browser_fallback) else WeiboAdapter(client, settings.weibo_cookie, settings.native_api_timeout_seconds),
        Platform.BILIBILI: BilibiliAdapter(client, settings.bilibili_cookie, settings.native_api_timeout_seconds),
        Platform.X: BrowserSearchAdapter(client, Platform.X, "https://x.com/search?q={query}&src=typed_query", headless=settings.browser_headless, timeout_ms=settings.browser_timeout_ms, cdp_url=settings.browser_cdp_url, user_data_dir=settings.browser_user_data_dir, channel=settings.browser_channel) if settings.browser_fallback and not settings.x_bearer_token else XAdapter(client, settings.x_bearer_token),
        Platform.INSTAGRAM: BrowserSearchAdapter(client, Platform.INSTAGRAM, "https://www.instagram.com/explore/tags/{query}/", headless=settings.browser_headless, timeout_ms=settings.browser_timeout_ms, cdp_url=settings.browser_cdp_url, user_data_dir=settings.browser_user_data_dir, channel=settings.browser_channel) if settings.browser_fallback and not settings.instagram_access_token else InstagramAdapter(client, settings.instagram_access_token, settings.instagram_user_id, settings.meta_graph_version),
        Platform.OTHER: WebPageAdapter(client),
    }
