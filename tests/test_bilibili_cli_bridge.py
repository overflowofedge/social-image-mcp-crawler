import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from social_image_mcp.models import CreatorFetchRequest, CreatorIdentity, ImageCandidate, Platform


_SPEC = importlib.util.spec_from_file_location(
    "bilibili_cli_bridge",
    Path(__file__).resolve().parents[1] / "scripts" / "bilibili_cli_bridge.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_bilibili_cli_collects_multiple_creator_pages(monkeypatch):
    class FakeApi:
        def __init__(self):
            self.requests = []

        async def fetch_creator(self, request):
            self.requests.append(request)
            page = len(self.requests)
            count = min(50, request.max_posts)
            return SimpleNamespace(
                identity=CreatorIdentity(
                    platform=Platform.BILIBILI, requested_id="42", canonical_id="42",
                    name="UP主", source="test", matched_by="exact_uid",
                ),
                items=(ImageCandidate(
                    id=f"video-{page}", platform=Platform.BILIBILI,
                    image_url=f"https://cdn.test/{page}.jpg", post_id=f"post-{page}",
                ),),
                posts_fetched=count,
                next_cursor=f'{{"native":"{page + 1}","offset":0}}' if page < 3 else None,
                post_ids=(f"post-{page}",),
                rejected_posts=0,
                pages_fetched=1,
                warnings=(),
            )

    monkeypatch.setenv("BILIBILI_CREATOR_SLEEP_SECONDS", "0.1")
    api = FakeApi()
    request = CreatorFetchRequest(
        platform=Platform.BILIBILI, creator_id="42", max_posts=120,
        max_images=1000, download=False,
    )

    result = asyncio.run(_MODULE._fetch_creator_pages(api, request))

    assert [item.max_posts for item in api.requests] == [120, 70, 20]
    assert result["posts_fetched"] == 120
    assert result["pages_fetched"] == 3
    assert result["post_ids"] == ["post-1", "post-2", "post-3"]
    assert len(result["items"]) == 3


def test_bilibili_cli_keeps_completed_pages_when_a_later_page_fails(monkeypatch):
    class FakeApi:
        calls = 0

        async def fetch_creator(self, request):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("temporary page failure")
            return SimpleNamespace(
                identity=CreatorIdentity(
                    platform=Platform.BILIBILI, requested_id="42", canonical_id="42",
                    name="UP主", source="test", matched_by="exact_uid",
                ),
                items=(ImageCandidate(
                    id="video-1", platform=Platform.BILIBILI,
                    image_url="https://cdn.test/1.jpg", post_id="post-1",
                ),),
                posts_fetched=50,
                next_cursor='{"native":"2","offset":0}',
                post_ids=("post-1",), rejected_posts=0, pages_fetched=1, warnings=(),
            )

    monkeypatch.setenv("BILIBILI_CREATOR_SLEEP_SECONDS", "0.1")
    result = asyncio.run(_MODULE._fetch_creator_pages(FakeApi(), CreatorFetchRequest(
        platform=Platform.BILIBILI, creator_id="42", max_posts=100,
        max_images=100, download=False,
    )))

    assert result["posts_fetched"] == 50
    assert result["post_ids"] == ["post-1"]
    assert any("temporary page failure" in warning for warning in result["warnings"])
