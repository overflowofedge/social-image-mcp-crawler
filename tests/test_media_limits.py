import pytest

from social_image_mcp.creator_service import CreatorImageService
from social_image_mcp.models import CreatorFetchRequest, DownloadRequest, ImageCandidate, SearchRequest
from social_image_mcp.service import SocialImageService


@pytest.mark.parametrize("route", ["search", "creator"])
@pytest.mark.parametrize(("media_type", "expected"), [
    ("images", ["photo-1", "photo-other"]),
    ("videos", ["video-1", "video-2"]),
    ("all", ["video-1", "photo-1", "video-2", "photo-other"]),
])
def test_image_limit_per_post_never_consumes_or_suppresses_video_quota(route, media_type, expected):
    items = [
        ImageCandidate(id=name, platform="douyin", image_url=f"https://cdn.test/{name}",
                       media_type=kind, post_id=post)
        for name, kind, post in [
            ("video-1", "video", "mixed"),
            ("photo-1", "image", "mixed"),
            ("photo-2", "image", "mixed"),
            ("video-2", "video", "mixed"),
            ("photo-other", "image", "other"),
            ("video-3", "video", "other"),
        ]
    ]
    if route == "search":
        request = SearchRequest(query="example", media_type=media_type, image_limit=2,
                                video_limit=2, per_post_limit=1)
        selected = SocialImageService._apply_media_limits(items, request)
    else:
        request = CreatorFetchRequest(platform="douyin", creator_id="123", media_type=media_type,
                                      max_images=2, max_videos=2, per_post_limit=1)
        selected = CreatorImageService._select_with_quotas(items, request)
    assert [item.id for item in selected] == expected


def test_large_desktop_limits_are_validated_consistently():
    search = SearchRequest(
        query="example", media_type="all", max_results=2000,
        image_limit=1000, video_limit=1000, max_posts=500,
    )
    creator = CreatorFetchRequest(
        platform="douyin", creator_id="123", max_posts=500,
        max_images=1000, max_videos=1000,
    )
    item = ImageCandidate(id="one", platform="douyin", image_url="https://cdn.test/one.jpg")

    assert (search.max_results, search.max_posts) == (2000, 500)
    assert (creator.max_images, creator.max_videos) == (1000, 1000)
    assert DownloadRequest(items=[item] * 2000).items[-1].id == "one"
    with pytest.raises(ValueError):
        SearchRequest(query="example", media_type="images", max_results=1001)
