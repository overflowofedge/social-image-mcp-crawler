import importlib.util
import io
import json
import os
import subprocess
import urllib.error
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_sources", ROOT / "scripts/bootstrap_sources.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SOURCE = {"name": "dy-cli", "repository": "example/douyin", "revision": "a" * 40, "entrypoint": "src/dy_cli/main.py"}


@pytest.fixture(autouse=True)
def compatibility_is_tested_separately(monkeypatch):
    monkeypatch.setattr(MODULE, "apply_compatibility", lambda *args: None)


def _archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, contents in entries.items():
            bundle.writestr(name, contents)
    stream.seek(0)
    return stream


def test_zip_install_downloads_missing_cli_without_git_and_preserves_it(monkeypatch, tmp_path):
    urls = []
    def download(request, **kwargs):
        urls.append(request.full_url)
        return _archive({"source-commit/src/dy_cli/main.py": "original"})
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", download)
    report = MODULE.install_sources(tmp_path, [SOURCE])
    assert report["ok"]
    entry = tmp_path / "dy-cli/src/dy_cli/main.py"
    entry.write_text("user patch", encoding="utf-8")
    assert MODULE.install_sources(tmp_path, [SOURCE])["sources"][0]["state"] == "preserved"
    assert entry.read_text(encoding="utf-8") == "user patch"
    assert urls == [f"https://codeload.github.com/example/douyin/zip/{'a' * 40}"]
    assert not list(tmp_path.glob(".source-install-*"))


def test_incomplete_source_is_not_overwritten(monkeypatch, tmp_path):
    existing = tmp_path / "dy-cli/user-data.txt"
    existing.parent.mkdir()
    existing.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("must not overwrite a partial source"))
    report = MODULE.install_sources(tmp_path, [SOURCE])
    assert report["ok"] is False
    assert existing.read_text(encoding="utf-8") == "keep"


def test_failed_source_retries_and_does_not_prevent_other_source_install(monkeypatch, tmp_path):
    calls = []
    def download(request, **kwargs):
        calls.append(request.full_url)
        if "/example/douyin/" in request.full_url:
            raise urllib.error.URLError("network interrupted")
        return _archive({"source-commit/main.py": "ok"})
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", download)
    other = {**SOURCE, "name": "MediaCrawler", "repository": "example/weibo", "entrypoint": "main.py"}
    report = MODULE.install_sources(tmp_path, [SOURCE, other])
    assert report["ok"] is False
    assert len(calls) == 4
    assert report["sources"][1]["ok"] is True
    assert (tmp_path / "MediaCrawler/main.py").is_file()
    assert not (tmp_path / "dy-cli").exists()


def test_archive_must_have_the_expected_cli_entrypoint(monkeypatch, tmp_path):
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *args, **kwargs: _archive({"source-commit/README.md": "not a CLI"}))
    assert MODULE.install_sources(tmp_path, [SOURCE])["ok"] is False
    assert not (tmp_path / "dy-cli").exists()


def test_all_shipped_sources_are_pinned_and_include_douyin():
    sources = MODULE.load_sources(ROOT / "scripts/sources.lock.json")
    assert {row["name"] for row in sources} == {"dy-cli", "MediaCrawler", "XHS-Downloader"}


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell compatibility")
def test_install_start_and_login_scripts_parse_in_windows_powershell_5():
    scripts = str(ROOT / "scripts").replace("'", "''")
    command = (
        "$failures = @(); Get-ChildItem -LiteralPath '" + scripts + "' -Filter *.ps1 | ForEach-Object { "
        "$tokens = $null; $errors = $null; "
        "[Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors) { $failures += $_.Name; $errors | ForEach-Object { Write-Output $_.Message } } }; "
        "if ($failures.Count) { Write-Output $failures; exit 1 }"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
