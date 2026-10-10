"""Install pinned CLI sources from GitHub archives, including ZIP-only installs.

Uses only the standard library and preserves existing sources and local patches.
Each source is installed separately so a failed download cannot discard another.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_compat import apply_compatibility

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_sources(lock_file: Path) -> list[dict[str, str]]:
    sources = json.loads(lock_file.read_text(encoding="utf-8"))["sources"]
    for source in sources:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source["name"]):
            raise ValueError("Invalid source directory name")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", source["repository"]):
            raise ValueError("Invalid GitHub repository")
        if not re.fullmatch(r"[a-f0-9]{40}", source["revision"]):
            raise ValueError("Source revisions must be pinned commit IDs")
        entry = PurePosixPath(source["entrypoint"])
        if entry.is_absolute() or ".." in entry.parts or ":" in str(entry):
            raise ValueError("Invalid source entrypoint")
    return sources


def extract_archive(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or ":" in member.filename or "\\" in member.filename:
                raise ValueError("Invalid path in source archive")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Symbolic links are not supported in source archives")
        bundle.extractall(destination)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Source archive must contain one source directory")
    return roots[0]


def install_source(source: dict[str, str], root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    target = root / source["name"]
    if (target / source["entrypoint"]).is_file():
        apply_compatibility(source["name"], target)
        return {"name": source["name"], "ok": True, "state": "preserved", "path": str(target)}
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RuntimeError(f"{target} is incomplete; move it aside and rerun installation. Existing files were preserved.")
    url = f"https://codeload.github.com/{source['repository']}/zip/{source['revision']}"
    with tempfile.TemporaryDirectory(prefix=".source-install-", dir=root) as temporary:
        staging = Path(temporary)
        archive = staging / "source.zip"
        for attempt in range(3):
            try:
                print(f"Downloading {source['name']} ({source['revision'][:12]}), attempt {attempt + 1}/3", flush=True)
                request = urllib.request.Request(url, headers={"User-Agent": "social-image-crawler-installer"})
                with urllib.request.urlopen(request, timeout=45) as response, archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
                extracted = extract_archive(archive, staging / f"extract-{attempt}")
                if not (extracted / source["entrypoint"]).is_file():
                    raise ValueError(f"Missing {source['entrypoint']} in downloaded source")
                apply_compatibility(source["name"], extracted)
                break
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                if attempt == 2:
                    raise RuntimeError(f"Could not install {source['name']}: {exc}") from exc
        if target.exists():
            target.rmdir()  # Only an empty, explicitly resolved source directory.
        shutil.move(str(extracted), str(target))
    return {"name": source["name"], "ok": True, "state": "installed", "revision": source["revision"], "path": str(target)}


def install_sources(root: Path, sources: list[dict[str, str]]) -> dict[str, Any]:
    results = []
    for source in sources:
        try:
            result = install_source(source, root)
        except (OSError, RuntimeError, ValueError) as exc:
            result = {"name": source["name"], "ok": False, "state": "failed", "detail": str(exc)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return {"ok": all(result["ok"] for result in results), "sources": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / "third_party")
    parser.add_argument("--source", action="append", choices=[row["name"] for row in load_sources(PROJECT_ROOT / "scripts/sources.lock.json")])
    args = parser.parse_args()
    sources = load_sources(PROJECT_ROOT / "scripts/sources.lock.json")
    if args.source:
        sources = [row for row in sources if row["name"] in args.source]
    report = install_sources(args.root.expanduser().resolve(), sources)
    report_path = PROJECT_ROOT / ".cache/source-install-latest.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
