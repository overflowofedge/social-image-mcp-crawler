import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_compat", ROOT / "scripts/source_compat.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_xhs_multiline_f_string_runs_on_python_310_without_changing_release(tmp_path):
    path = tmp_path / "source/module/static.py"
    path.parent.mkdir(parents=True)
    path.write_text('VERSION_MAJOR = 2\nVERSION_MINOR = 8\nVERSION_BETA = False\n# local customization\nPROJECT = f"XHS-Downloader V{VERSION_MAJOR}.{VERSION_MINOR} {\n    \'Beta\' if VERSION_BETA else \'Stable\'\n}"\n', encoding="utf-8")
    MODULE.apply_compatibility("XHS-Downloader", tmp_path)
    namespace = {}
    exec(path.read_text(encoding="utf-8"), namespace)
    assert namespace["PROJECT"] == "XHS-Downloader V2.8 Stable"
    assert "# local customization" in path.read_text(encoding="utf-8")
    first = path.read_bytes()
    MODULE.apply_compatibility("XHS-Downloader", tmp_path)
    assert path.read_bytes() == first


def test_fresh_dy_cli_resolves_hyphenated_profile_shares_after_install(tmp_path):
    path = tmp_path / "src/dy_cli/engines/api_client.py"
    path.parent.mkdir(parents=True)
    path.write_text('import re\nSHORT_URL_PATTERN = re.compile(r"https?://v\\.douyin\\.com/\\w+/?")\nclass DouyinAPIClient:\n    def resolve_share_url(self, url: str) -> str:\n        return "existing video behavior"\n', encoding="utf-8")
    MODULE.apply_compatibility("dy-cli", tmp_path)
    namespace = {}
    exec(path.read_text(encoding="utf-8"), namespace)
    client = namespace["DouyinAPIClient"]()
    client._short_url_redirects = lambda url: (["https://www.iesdouyin.com/share/user/MS4w-test/"], "", "")
    assert client.resolve_creator_share_url("https://v.douyin.com/Ab-c_d/?from=share") == "https://www.douyin.com/user/MS4w-test"
    assert client.resolve_share_url("video") == "existing video behavior"
    assert client.resolve_creator_share_url("https://www.douyin.com/user/MS4w-direct") == "https://www.douyin.com/user/MS4w-direct"
    first = path.read_bytes()
    MODULE.apply_compatibility("dy-cli", tmp_path)
    assert path.read_bytes() == first


def test_existing_creator_method_does_not_skip_legacy_short_link_repair(tmp_path):
    path = tmp_path / "src/dy_cli/engines/api_client.py"
    path.parent.mkdir(parents=True)
    path.write_text('import re\nSHORT_URL_PATTERN = re.compile(r"https?://v\\.douyin\\.com/\\w+/?")\n# custom local method\nclass DouyinAPIClient:\n    def resolve_creator_share_url(self, url):\n        return "custom result"\n', encoding="utf-8")
    MODULE.apply_compatibility("dy-cli", tmp_path)
    namespace = {}
    exec(path.read_text(encoding="utf-8"), namespace)
    assert namespace["SHORT_URL_PATTERN"].fullmatch("https://v.douyin.com/Ab-c_d/?from=share")
    assert namespace["DouyinAPIClient"]().resolve_creator_share_url("test") == "custom result"
    assert "# custom local method" in path.read_text(encoding="utf-8")
    first = path.read_bytes()
    MODULE.apply_compatibility("dy-cli", tmp_path)
    assert path.read_bytes() == first
