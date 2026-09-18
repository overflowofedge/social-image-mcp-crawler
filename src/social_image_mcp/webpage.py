from __future__ import annotations

import hashlib
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .adapters.base import AdapterError, AdapterStatus, PlatformAdapter
from .intent import Intent
from .models import ImageCandidate, Platform


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")
_SKIP_PATH_PARTS = ("avatar", "favicon", "logo", "icon", "sprite", "emoji")


def _srcset_urls(value: str) -> list[str]:
    values: list[tuple[float, str]] = []
    for part in value.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        weight = 0.0
        if len(tokens) > 1:
            marker = tokens[1].lower()
            try:
                weight = float(marker[:-1]) if marker.endswith("w") else float(marker[:-1]) * 1000 if marker.endswith("x") else 0.0
            except ValueError:
                weight = 0.0
        values.append((weight, tokens[0]))
    return [url for _, url in sorted(values, reverse=True)]


class WebPageAdapter(PlatformAdapter):
    """Fetch image references from an explicitly supplied public web page."""

    platform = Platform.OTHER

    @property
    def status(self) -> AdapterStatus:
        return AdapterStatus(self.platform, True, "webpage", "Extracts image URLs from a supplied http(s) page URL")

    @staticmethod
    def _is_valid_page_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _is_image_url(value: str) -> bool:
        lowered = value.lower().split("?", 1)[0].split("#", 1)[0]
        return lowered.endswith(_IMAGE_EXTENSIONS) or any(token in lowered for token in ("image", "photo", "picture", "media"))

    async def search(self, intent: Intent, limit: int, safe_mode: bool) -> list[ImageCandidate]:
        page_url = intent.url
        if not page_url or not self._is_valid_page_url(page_url):
            raise AdapterError("其他平台需要输入完整的 http(s) 网页链接")
        try:
            response = await self.client.get(
                page_url,
                headers={"User-Agent": "Mozilla/5.0 (social-image-mcp webpage extractor)"},
                follow_redirects=True,
                timeout=20,
            )
            response.raise_for_status()
        except Exception as exc:
            raise AdapterError(f"网页访问失败：{exc}") from exc
        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith(("text/html", "application/xhtml")):
            raise AdapterError("网址没有返回 HTML 网页")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        candidates: list[tuple[str, str]] = []

        def add(value: str | None, alt: str = "") -> None:
            if not value or value.startswith(("data:", "blob:")):
                return
            absolute = urljoin(str(response.url), value.strip())
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return
            path = parsed.path.lower()
            if any(part in path for part in _SKIP_PATH_PARTS) and not self._is_image_url(absolute):
                return
            if absolute not in {item[0] for item in candidates}:
                candidates.append((absolute, alt))

        for selector in (
            ("meta[property='og:image']", "content"),
            ("meta[name='twitter:image']", "content"),
            ("link[rel='image_src']", "href"),
        ):
            for node in soup.select(selector[0]):
                add(node.get(selector[1]), "页面主图")
        for node in soup.find_all("img"):
            alt = node.get("alt") or node.get("title") or ""
            values = [node.get(key) for key in ("src", "data-src", "data-original", "data-lazy-src")]
            for value in values:
                add(value, alt)
            for value in _srcset_urls(node.get("srcset", "")):
                add(value, alt)
        result: list[ImageCandidate] = []
        for index, (image_url, alt) in enumerate(candidates[: max(1, limit)], 1):
            digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:16]
            result.append(ImageCandidate(
                id=f"web-{digest}", platform=Platform.OTHER, image_url=image_url,
                permalink=str(response.url), title=alt or title or str(response.url),
                description=alt or title, alt_text=alt, media_type="image",
                source_payload={"source": "webpage", "page_url": str(response.url), "index": index},
            ))
        if not result:
            raise AdapterError("网页中没有找到可下载的图片")
        return result
