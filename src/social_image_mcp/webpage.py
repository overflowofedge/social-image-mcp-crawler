from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from PIL import ImageFile

from .adapters.base import AdapterError, AdapterStatus, PlatformAdapter
from .intent import Intent
from .models import ImageCandidate, Platform, SearchRequest


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")
_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".ogv", ".ogg")
_STREAM_EXTENSIONS = (".m3u8", ".mpd")
_UI = re.compile(
    r"(?:^|[\W_])(?:logo|icons?|favicon|avatar|sprite|emoji|placeholder|loading|loader|"
    r"spacer|pixel|tracking|qrcode|qr-code|telegram|wechat|close|lock|coin|share|"
    r"fenxiang|age-gate|badges?|lockup)(?:$|[\W_])|图标|头像|二维码|网站标志", re.I,
)
_CHROME = re.compile(r"(?:^|[\W_])(?:navbar|navigation|sidebar|toolbar|social|advert|advertisement|ads|banner-ad|modal|popup|cookie-banner)(?:$|[\W_])", re.I)
_CONTENT = re.compile(r"(?:^|[\W_])(?:post|article|entry|gallery|video|photo|card|portfolio|work|content)(?:$|[\W_])", re.I)
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
MIN_CONTENT_SIDE = 160


def _url(base: str, value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        absolute = urldefrag(urljoin(base, value.strip()))[0]
        parsed = urlparse(absolute)
        return absolute if parsed.scheme in {"http", "https"} and parsed.hostname else None
    except ValueError:
        return None


def _path(url: str) -> str:
    return unquote(urlparse(url).path).lower()


def _srcset_urls(value: str) -> list[str]:
    values = []
    for part in value.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        try:
            weight = float(tokens[1][:-1]) if len(tokens) > 1 else 0
        except ValueError:
            weight = 0
        values.append((weight, tokens[0]))
    return [url for _, url in sorted(values, reverse=True)]


def _label(node: Tag) -> str:
    return " ".join(str(node.get(key) or "") for key in ("id", "class", "alt", "title", "aria-label", "role"))


def _image_source(node: Tag, base: str) -> str | None:
    """Choose the content source, excluding placeholder and UI-only images."""
    choices = [node.get(key) for key in ("data-original", "data-full", "data-large")]
    picture = node.find_parent("picture")
    if picture:
        for source in picture.find_all("source"):
            choices += _srcset_urls(str(source.get("data-srcset") or source.get("srcset") or ""))
    choices += _srcset_urls(str(node.get("data-srcset") or node.get("srcset") or ""))
    choices += [node.get(key) for key in ("data-src", "data-lazy-src", "src")]
    for value in choices:
        absolute = _url(base, value)
        if absolute and not _UI.search(_path(absolute)) and not _path(absolute).endswith((".svg", ".ico")):
            return absolute
    return None


def _is_content_card(node: Tag, base: str) -> bool:
    images = node.find_all("img")
    if images:
        # A link containing only an icon or product badge is navigation.
        return any(not _is_chrome(image) and _image_source(image, base) for image in images)
    return bool(node.find("video") or _CONTENT.search(_label(node.parent)) or node.find(["h2", "h3"]))


def _is_chrome(node: Tag) -> bool:
    for parent in [node, *node.parents]:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"nav", "footer", "aside", "button"}:
            return True
        if parent.name == "header" and not parent.find_parent(["main", "article"]):
            return True
        if parent.get("role") in {"navigation", "banner", "contentinfo", "dialog"}:
            return True
        if parent.has_attr("hidden") or parent.get("aria-hidden") == "true":
            return True
        style = re.sub(r"\s+", "", str(parent.get("style") or "")).lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if _CHROME.search(_label(parent)) or _UI.search(_label(parent)):
            return True
    return False


@dataclass
class PageMedia:
    items: list[ImageCandidate] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract_page(html: str, page_url: str) -> PageMedia:
    """Extract content media, never navigation assets or player HTML URLs."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    base_tag = soup.find("base", href=True)
    base = (_url(page_url, base_tag.get("href")) if base_tag else page_url) or page_url
    result = PageMedia()
    seen: set[str] = set()
    scopes = soup.select("main, [role='main']") or soup.select("article") or [soup.body or soup]

    def add(value, kind="image", alt="", poster=None, post_url=None, original=False) -> None:
        absolute = _url(base, value)
        if not absolute or absolute in seen or _UI.search(_path(absolute)) or _UI.search(alt):
            return
        if re.search(r"/static/.*(?:/common/|/ai/)", _path(absolute)):
            return
        if _path(absolute).endswith(_STREAM_EXTENSIONS):
            result.warnings.append("页面含 HLS/DASH 分段视频，当前仅下载可直接访问的 MP4/WebM 等视频文件。")
            return
        if kind == "image" and _path(absolute).endswith((".svg", ".ico", *_VIDEO_EXTENSIONS)):
            return
        seen.add(absolute)
        digest = hashlib.sha1(absolute.encode()).hexdigest()[:16]
        post = post_url or page_url
        result.items.append(ImageCandidate(
            id=f"web-{digest}", platform=Platform.OTHER, image_url=absolute,
            media_type=kind, permalink=page_url, thumbnail_url=_url(base, poster),
            title=alt or title or page_url, description=alt or title, alt_text=alt,
            post_id=hashlib.sha1(post.encode()).hexdigest()[:16],
            source_payload={"source": "webpage", "page_url": page_url, "content_page": post, "original_image": original},
        ))

    def content_link(node: Tag) -> str | None:
        href = str(node.get("href") or "")
        if not href or href.startswith("#"):
            return None
        absolute = _url(base, href)
        if not absolute or absolute == page_url or urlparse(absolute).netloc != urlparse(page_url).netloc:
            return None
        if _path(absolute).endswith((*_IMAGE_EXTENSIONS, *_VIDEO_EXTENSIONS, *_STREAM_EXTENSIONS)):
            return None
        if re.search(r"/(?:login|signup|register|search|tags?|category|privacy|terms|contact|about)(?:/|$)", _path(absolute)):
            return None
        return absolute

    for scope in scopes:
        for node in scope.find_all(["img", "video", "a", "div", "figure"]):
            if _is_chrome(node):
                continue
            alt = str(node.get("alt") or node.get("title") or "")
            if node.name == "img":
                anchor = node.find_parent("a", href=True)
                post = content_link(anchor) if anchor else None
                linked = _url(base, anchor.get("href")) if anchor else None
                original = bool(linked and _path(linked).endswith(_IMAGE_EXTENSIONS))
                chosen = linked if original else _image_source(node, base)
                if chosen:
                    original |= any(chosen == _url(base, node.get(key)) for key in ("data-original", "data-full", "data-large"))
                    add(chosen, alt=alt, post_url=post, original=original)
            elif node.name == "video":
                sources = [node.get("data-src"), node.get("src")]
                sources += [source.get("src") or source.get("data-src") for source in node.find_all("source")]
                for value in sources:
                    if absolute := _url(base, value):
                        add(value, "video", alt, node.get("poster"))
                        if not _path(absolute).endswith(_STREAM_EXTENSIONS):
                            break
            elif node.name == "a":
                absolute = _url(base, node.get("href"))
                if absolute and _path(absolute).endswith((*_VIDEO_EXTENSIONS, *_STREAM_EXTENSIONS)):
                    add(absolute, "video", node.get_text(" ", strip=True))
                elif absolute and _path(absolute).endswith(_IMAGE_EXTENSIONS):
                    child = node.find("img")
                    child_alt = str(child.get("alt") or child.get("title") or "") if child else ""
                    add(absolute, alt=child_alt or alt, original=True)
                elif _is_content_card(node, base):
                    if (link := content_link(node)) and link not in result.links:
                        result.links.append(link)
            elif _CONTENT.search(_label(node)):
                match = re.search(r"background(?:-image)?\s*:[^;]*url\(['\"]?([^)'\"]+)", str(node.get("style") or ""), re.I)
                if match:
                    add(match.group(1), alt=alt)

    def structured(value) -> None:
        if isinstance(value, list):
            for child in value:
                structured(child)
        elif isinstance(value, dict):
            kind = value.get("@type")
            types = kind if isinstance(kind, list) else [kind]
            if "VideoObject" in types:
                add(value.get("contentUrl"), "video", str(value.get("name") or ""), value.get("thumbnailUrl"))
            elif "ImageObject" in types:
                add(value.get("contentUrl") or value.get("url"), alt=str(value.get("caption") or value.get("name") or ""))
            for key, child in value.items():
                if key not in {"logo", "publisher", "author", "thumbnailUrl"}:
                    structured(child)

    for node in soup.select("script[type='application/ld+json']"):
        try:
            structured(json.loads(node.string or node.get_text()))
        except (ValueError, TypeError, RecursionError):
            continue
    # Website branding in social preview metadata must not displace content.
    blocked_images = {
        _url(base, node.get(key))
        for node in soup.find_all("img") if _is_chrome(node)
        for key in ("src", "data-src", "data-original")
    }
    if not any(item.media_type == "image" for item in result.items):
        for node in soup.select("meta[property='og:image'], meta[name='twitter:image'], link[rel='image_src']"):
            value = node.get("content") or node.get("href")
            if _url(base, value) not in blocked_images:
                add(value, alt=title)
    for node in soup.select("meta[property='og:video'], meta[property='og:video:url'], meta[property='og:video:secure_url']"):
        value = _url(base, node.get("content"))
        if value and _path(value).endswith((*_VIDEO_EXTENSIONS, *_STREAM_EXTENSIONS)):
            add(value, "video", title)
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


class WebPageAdapter(PlatformAdapter):
    platform = Platform.OTHER

    def __init__(self, client, *, render_pages=False, browser_channel=None, browser_path=None):
        super().__init__(client)
        self.render_pages = render_pages
        self.browser_channel = browser_channel
        self.browser_path = browser_path

    @property
    def status(self) -> AdapterStatus:
        return AdapterStatus(self.platform, True, "webpage", "Extracts content images and direct videos from a homepage and linked content pages")

    async def _page(self, url: str) -> tuple[str, PageMedia]:
        try:
            response = await self.client.get(url, headers=_HEADERS, follow_redirects=True, timeout=12)
            response.raise_for_status()
        except Exception as exc:
            raise AdapterError(f"网页访问失败：{exc}") from exc
        if not response.headers.get("content-type", "").lower().startswith(("text/html", "application/xhtml")):
            raise AdapterError("网址没有返回 HTML 网页")
        return str(response.url), extract_page(response.text, str(response.url))

    async def _image_dimensions(self, item: ImageCandidate) -> ImageCandidate | None:
        # Read enough bytes for dimensions, rather than the full original.
        try:
            headers = {**_HEADERS, "Referer": item.permalink or "", "Range": "bytes=0-262143"}
            async with self.client.stream("GET", item.image_url, headers=headers, follow_redirects=True, timeout=5) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";", 1)[0]
                if mime and not (mime.startswith("image/") or mime == "application/octet-stream"):
                    return None
                parser = ImageFile.Parser()
                size = 0
                async for chunk in response.aiter_bytes(16384):
                    parser.feed(chunk)
                    size += len(chunk)
                    if parser.image:
                        width, height = parser.image.size
                        if min(width, height) < MIN_CONTENT_SIDE:
                            return None
                        return item.model_copy(update={"width": width, "height": height})
                    if size >= 262144:
                        break
        except Exception:
            return None
        return None

    async def _render_page(self, url: str) -> PageMedia:
        from playwright.async_api import async_playwright

        options = {"headless": True}
        if self.browser_path and Path(self.browser_path).is_file():
            options["executable_path"] = self.browser_path
        elif self.browser_channel:
            options["channel"] = self.browser_channel
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**options)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                await page.goto(url, wait_until="domcontentloaded", timeout=6000)
                for _ in range(3):
                    await page.wait_for_timeout(500)
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                # Preserve lazy-loaded media selected by the browser itself.
                await page.evaluate("""() => {
                    for (const el of document.querySelectorAll('img, video')) {
                        if (el.currentSrc && !el.currentSrc.startsWith('blob:')) {
                            el.setAttribute('data-src', el.currentSrc);
                        }
                    }
                }""")
                if urlparse(page.url).netloc != urlparse(url).netloc:
                    raise AdapterError("动态页面跳转到其他站点，已停止提取。")
                return extract_page(await page.content(), page.url)
            finally:
                await browser.close()

    async def _video_file(self, item: ImageCandidate) -> ImageCandidate | None:
        try:
            headers = {**_HEADERS, "Referer": item.permalink or "", "Range": "bytes=0-255"}
            async with self.client.stream("GET", item.image_url, headers=headers, follow_redirects=True, timeout=5) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if mime.startswith("video/") and "mpegurl" not in mime:
                    return item
                if mime in {"", "application/octet-stream"}:
                    async for chunk in response.aiter_bytes(256):
                        if chunk[4:8] == b"ftyp" or chunk.startswith((b"\x1aE\xdf\xa3", b"OggS")):
                            return item
                        break
        except Exception:
            return None
        return None

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        result = await self.search_media(SearchRequest(query=intent.raw, max_results=min(limit, 200), safe_mode=safe_mode))
        return result.items

    async def search_media(self, request: SearchRequest) -> PageMedia:
        deadline = asyncio.get_running_loop().time() + 42
        page_url = _url(request.query, request.query)
        if not page_url:
            raise AdapterError("其他平台需要输入完整的 http(s) 网页链接")
        try:
            page_url, root = await asyncio.wait_for(self._page(page_url), timeout=12)
        except asyncio.TimeoutError as exc:
            raise AdapterError("主页访问超时，请稍后重试。") from exc
        warnings = list(root.warnings)
        needs_render = not root.items and not root.links
        needs_render |= request.media_type != "images" and not root.links and not any(item.media_type == "video" for item in root.items)
        if self.render_pages and needs_render:
            try:
                rendered = await asyncio.wait_for(self._render_page(page_url), timeout=9)
                root.items.extend(rendered.items)
                root.links.extend(link for link in rendered.links if link not in root.links)
                warnings.extend(rendered.warnings)
            except Exception:
                warnings.append("动态页面读取未完成；请确认浏览器组件已安装，且页面可公开访问。")
        # One level of content cards only; never recursively crawl navigation.
        links = root.links[:request.max_posts]
        pages: dict[str, PageMedia] = {}
        semaphore = asyncio.Semaphore(4)

        async def detail(url):
            async with semaphore:
                try:
                    resolved, media = await self._page(url)
                    if urlparse(resolved).netloc == urlparse(page_url).netloc:
                        # Rendering every card is costly; inspect at most the
                        # first two pages whose requested media is dynamic.
                        missing = not media.items or (request.media_type != "images" and not any(item.media_type == "video" for item in media.items))
                        if self.render_pages and missing and url in links[:2]:
                            try:
                                rendered = await asyncio.wait_for(self._render_page(resolved), timeout=8)
                                media.items.extend(rendered.items)
                                media.warnings.extend(rendered.warnings)
                            except Exception:
                                warnings.append("部分动态详情页无法读取，保留已找到的媒体。")
                        pages[url] = media
                    else:
                        warnings.append("已跳过跳转到其他站点的详情页。")
                except AdapterError:
                    warnings.append("部分内容详情页访问失败，保留其他页面的结果。")

        if links:
            try:
                budget = max(0.1, min(20, deadline - asyncio.get_running_loop().time() - 6))
                await asyncio.wait_for(asyncio.gather(*(detail(link) for link in links)), timeout=budget)
            except asyncio.TimeoutError:
                warnings.append("详情页检索达到时限，已保留完成的结果。")
        pool = []
        for link in links:
            if link in pages:
                pool.extend(pages[link].items)
                warnings.extend(pages[link].warnings)
        pool.extend(root.items)
        image_limit = request.image_limit or request.max_results
        video_limit = request.video_limit or request.max_results
        unique = list({item.image_url: item for item in reversed(pool)}.values())[::-1]
        images = [item for item in unique if item.media_type == "image"] if request.media_type != "videos" else []
        # Explicit links to originals must be considered before gallery
        # thumbnails, otherwise a short quota can omit every full-size image.
        images.sort(key=lambda item: not item.source_payload.get("original_image", False))
        images = images[: min(400, image_limit * 3)]
        videos = [item for item in unique if item.media_type == "video"][: min(200, video_limit * 3)] if request.media_type != "images" else []
        verified: dict[str, ImageCandidate] = {}
        semaphore = asyncio.Semaphore(8)

        async def verify(item):
            async with semaphore:
                found = await (self._image_dimensions(item) if item.media_type == "image" else self._video_file(item))
                if found and found.media_type == "image" and ((found.width or 0) < request.min_width or (found.height or 0) < request.min_height):
                    found = None
                if found:
                    verified[item.image_url] = found

        if images or videos:
            try:
                budget = max(0.1, min(12, deadline - asyncio.get_running_loop().time()))
                await asyncio.wait_for(asyncio.gather(*(verify(item) for item in images + videos)), timeout=budget)
            except asyncio.TimeoutError:
                warnings.append("媒体校验达到时限，已保留验证通过的结果。")
        candidates = [verified[item.image_url] for item in images + videos if item.image_url in verified]
        replaced = {link for link, media in pages.items() if any(
            item.media_type == "image" and item.image_url in verified for item in media.items
        )}
        candidates = [item for item in candidates if not (
            item.permalink == page_url and item.source_payload["content_page"] in replaced
        )]
        selected = []
        counts = {"image": 0, "video": 0}
        posts: dict[str, int] = {}
        for item in candidates:
            maximum = image_limit if item.media_type == "image" else video_limit
            # The per-post image limit must not suppress a post's video.
            if counts[item.media_type] >= maximum or (item.media_type == "image" and request.per_post_limit and posts.get(item.post_id, 0) >= request.per_post_limit):
                continue
            counts[item.media_type] += 1
            if item.media_type == "image":
                posts[item.post_id] = posts.get(item.post_id, 0) + 1
            selected.append(item)
        if not selected:
            message = "没有找到可下载的正文图片或直接视频文件；已过滤图标、Logo 和小图。页面可能需要登录或动态加载。"
            if warnings:
                message += " " + " ".join(dict.fromkeys(warnings))
            raise AdapterError(message)
        if request.media_type == "all" and not counts["video"]:
            warnings.append("未找到可直接下载的视频文件，本次仅返回正文图片。")
        return PageMedia(selected, warnings=list(dict.fromkeys(warnings)))
