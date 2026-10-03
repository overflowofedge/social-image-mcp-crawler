from __future__ import annotations

"""Repair editable-install path encodings before Python imports site.

Run with ``python -S``: Python 3.10 can fail before running any user code
when pip wrote a .pth in the Windows code page and startup enables UTF-8.
ASCII escapes let the same environment work with either encoding mode.
"""

import argparse
import locale
import os
from pathlib import Path


def repair_pth(path: Path, legacy_encoding: str) -> bool:
    original = path.read_bytes()
    if original.isascii():
        return False
    try:
        content = original.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = original.decode(legacy_encoding)
        except UnicodeDecodeError as exc:
            raise ValueError(f"Cannot decode {path.name}; original file was left unchanged") from exc

    lines = []
    for line in content.splitlines():
        line = line.rstrip()
        if line.isascii() or line.startswith("#"):
            lines.append(line.encode("ascii", errors="backslashreplace").decode("ascii"))
        elif line.startswith(("import ", "import\t")):
            # Preserve existing startup statements without executing them
            # during repair. ascii() escapes only the source string literal.
            lines.append(f"import builtins; builtins.exec({ascii(line)})")
        else:
            target = str((path.parent / line).resolve())
            # Match .pth path semantics: skip missing and duplicate entries.
            lines.append(
                "import os, sys; "
                f"sys.path.extend(p for p in [{ascii(target)}] "
                "if os.path.exists(p) and os.path.normcase(p) not in "
                "{os.path.normcase(v) for v in sys.path})"
            )

    backup = path.with_suffix(path.suffix + ".encoding-backup")
    try:
        with backup.open("xb") as stream:
            stream.write(original)
    except FileExistsError:
        pass
    temporary = path.with_suffix(path.suffix + ".encoding-tmp")
    temporary.write_bytes(("\n".join(lines) + "\n").encode("ascii"))
    temporary.replace(path)
    return True


def repair_environment(venv: Path) -> int:
    site_packages = venv / "Lib" / "site-packages"
    if not (venv / "pyvenv.cfg").is_file() or not site_packages.is_dir():
        raise ValueError(f"Invalid virtual environment: {venv}")
    # mbcs uses the Windows ANSI code page even under PYTHONUTF8=1.
    legacy_encoding = "mbcs" if os.name == "nt" else locale.getpreferredencoding(False)
    return sum(repair_pth(path, legacy_encoding) for path in sorted(site_packages.glob("*.pth")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", required=True, type=Path)
    args = parser.parse_args()
    try:
        repaired = repair_environment(args.venv.resolve())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Python environment repair failed: {exc}\n")
    if repaired:
        print(f"Repaired {repaired} Python path file(s); original files backed up.")


if __name__ == "__main__":
    main()
