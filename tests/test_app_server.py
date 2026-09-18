import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "app_server",
    Path(__file__).resolve().parents[1] / "scripts" / "app_server.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_preview_referer = _MODULE._preview_referer


def test_preview_referer_uses_weibo_page_for_sina_image_hosts():
    assert _preview_referer("https://wx2.sinaimg.cn/mw2000/a.jpg") == "https://m.weibo.cn/"


def test_preview_referer_preserves_item_permalink():
    assert _preview_referer(
        "https://wx2.sinaimg.cn/mw2000/a.jpg",
        "https://m.weibo.cn/detail/123",
    ) == "https://m.weibo.cn/detail/123"
