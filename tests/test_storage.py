from pathlib import Path

from social_image_mcp.models import ImageCandidate, Platform
from social_image_mcp.storage import StorageAgent


def test_storage_agent_builds_platform_account_media_root(tmp_path):
    storage = StorageAgent(tmp_path / "downloads")
    item = ImageCandidate(
        id="post:1", platform=Platform.DOUYIN, image_url="https://cdn.test/a.jpg",
        creator_name="放学小野猪", post_id="post", media_index=1,
    )
    account_dir = storage.account_dir(item.platform, candidate=item)
    assert account_dir == tmp_path / "downloads" / "douyin" / "放学小野猪"


def test_storage_agent_writes_account_markdown_from_download_records(tmp_path):
    storage = StorageAgent(tmp_path / "downloads")
    account_dir = storage.account_dir(Platform.X, identity={"name": "NASA"})
    media_dir = account_dir / "images"
    media_dir.mkdir(parents=True)
    media_path = media_dir / "post.jpg"
    media_path.write_bytes(b"image")
    info_path = storage.write_index(
        account_dir,
        Platform.X,
        identity={"name": "NASA", "canonical_id": "NASA"},
        records=[{
            "candidate_id": "post:1", "platform": "x", "image_url": "https://pbs.test/post.jpg",
            "media_type": "image", "path": str(media_path), "sha256": "abc123",
            "post_id": "post", "media_index": 1, "title": "A | title",
            "published_at": "2026-10-03T12:34:56+08:00", "status": "downloaded",
        }],
    )
    assert info_path == account_dir / "NASA.md"
    text = info_path.read_text(encoding="utf-8")
    assert text.startswith("# 已下载图片清单")
    assert "只录入确认下载完成的图片" in text
    assert "本表是「已下载」的唯一权威依据" in text
    assert "- 2026-10-03 A \\| title post.jpg" in text
    assert "A \\| title" in text
    assert "| Published |" not in text


def test_storage_agent_writes_readable_mixed_media_list(tmp_path):
    storage = StorageAgent(tmp_path / "downloads")
    account_dir = storage.account_dir(Platform.DOUYIN, identity={"name": "示例账号"})
    image_path = account_dir / "images" / "20261001_cover.jpg"
    video_path = account_dir / "videos" / "20261002_work.mp4"
    image_path.parent.mkdir(parents=True)
    video_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    video_path.write_bytes(b"video")
    info_path = storage.write_index(
        account_dir,
        Platform.DOUYIN,
        records=[
            {
                "candidate_id": "image:1", "platform": "douyin", "media_type": "image",
                "path": str(image_path), "post_id": "image", "media_index": 1,
                "title": "封面", "published_at": None, "status": "downloaded",
            },
            {
                "candidate_id": "video:1", "platform": "douyin", "media_type": "video",
                "path": str(video_path), "post_id": "video", "media_index": 1,
                "title": "精彩视频", "published_at": "2026-10-02", "status": "downloaded",
            },
        ],
    )
    text = info_path.read_text(encoding="utf-8")
    assert text.startswith("# 已下载媒体清单")
    assert "确认下载完成的图片和视频" in text
    assert "- 2026-10-01 封面 20261001_cover.jpg" in text
    assert "- 2026-10-02 精彩视频 20261002_work.mp4" in text

