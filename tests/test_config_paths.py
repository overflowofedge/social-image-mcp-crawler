import sys

from social_image_mcp import config


def test_placeholder_cli_path_uses_current_project_and_python(monkeypatch):
    monkeypatch.setenv("DOUYIN_SOURCE_COMMAND", 'python "C:/path/to/project/scripts/douyin_cli_bridge.py"')
    command = config._source_command("DOUYIN_SOURCE_COMMAND", "douyin_cli_bridge.py")
    assert str(config.PROJECT_ROOT / "scripts/douyin_cli_bridge.py") in command
    assert sys.executable in command
    assert "C:/path/to/project" not in command


def test_explicit_custom_cli_is_preserved(monkeypatch):
    monkeypatch.setenv("DOUYIN_SOURCE_COMMAND", '"custom-python" "custom-bridge.py" --limit {limit}')
    assert config._source_command("DOUYIN_SOURCE_COMMAND", "douyin_cli_bridge.py") == '"custom-python" "custom-bridge.py" --limit {limit}'
