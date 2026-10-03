from social_image_mcp.server import _diagnostics


def test_diagnostics_exposes_mcp_tools_and_sources():
    result = _diagnostics()
    assert "search_images" in result["tools"]
    assert "list_sources" in result["tools"]
    assert {item["name"] for item in result["sources"]} == {"dy-cli", "bilibili-cli", "media-crawler", "xhs-downloader", "gallery-dl"}
    assert set((result["object_filter"] or {})) >= {"configured", "ready", "model"}
