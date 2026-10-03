import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_python_env import repair_pth


def test_path_repair_preserves_original_and_skips_missing_or_duplicate_paths(tmp_path):
    source = tmp_path / "中文目录"
    source.mkdir()
    pth = tmp_path / "editable.pth"
    original = f"# 路径\n{source}\n{source}\n不存在\n".encode("gb18030")
    pth.write_bytes(original)

    assert repair_pth(pth, "gb18030")
    assert pth.with_suffix(".pth.encoding-backup").read_bytes() == original
    result = subprocess.run(
        [sys.executable, "-S", "-c",
         "import site, sys; site.addsitedir(sys.argv[1]); "
         "assert sys.path.count(sys.argv[2]) == 1; "
         "assert sys.argv[3] not in sys.path",
         str(tmp_path), str(source), str(tmp_path / "不存在")],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    repaired = pth.read_bytes()
    assert repaired.isascii()
    assert not repair_pth(pth, "gb18030")
    assert pth.read_bytes() == repaired
    assert pth.with_suffix(".pth.encoding-backup").read_bytes() == original


def test_unicode_startup_code_runs_only_when_site_loads(tmp_path):
    marker = tmp_path / "启动标记"
    pth = tmp_path / "startup.pth"
    statement = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('完成', encoding='utf-8')\n"
    pth.write_bytes(statement.encode("utf-8"))

    assert repair_pth(pth, "gb18030")
    assert not marker.exists()
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import site, sys; site.addsitedir(sys.argv[1])", str(tmp_path)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "完成"


def test_ascii_pth_is_not_rewritten(tmp_path):
    pth = tmp_path / "dependency.pth"
    original = b"import os\r\nrelative/path\r\n"
    pth.write_bytes(original)
    assert not repair_pth(pth, "gb18030")
    assert pth.read_bytes() == original
    assert not pth.with_suffix(".pth.encoding-backup").exists()


def test_unknown_encoding_does_not_change_file(tmp_path):
    pth = tmp_path / "broken.pth"
    pth.write_bytes(b"\x81")
    with pytest.raises(ValueError, match="original file was left unchanged"):
        repair_pth(pth, "gb18030")
    assert pth.read_bytes() == b"\x81"


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop virtual environment")
@pytest.mark.parametrize("encoding", ["mbcs", "utf-8"])
def test_windows_environment_starts_in_both_encoding_modes_after_repair(tmp_path, encoding):
    project = tmp_path / "中文目录 空格"
    source = project / "src"
    source.mkdir(parents=True)
    (source / "encoding_probe.py").write_text("VALUE = 42\n", encoding="ascii")
    environment = project / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "Scripts" / "python.exe"
    pth = environment / "Lib" / "site-packages" / "editable.pth"
    try:
        path_bytes = (str(source) + "\n").encode(encoding)
    except UnicodeEncodeError:
        pytest.skip("The local Windows code page cannot encode Chinese paths")
    pth.write_bytes(path_bytes)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if encoding == "mbcs":
        broken = subprocess.run([str(python), "-c", "pass"], env=env, capture_output=True)
        if path_bytes != (str(source) + "\n").encode("utf-8"):
            assert broken.returncode != 0
            assert b"init_import_site" in broken.stderr
            assert b"UnicodeDecodeError" in broken.stderr

    helper = Path(__file__).resolve().parents[1] / "scripts" / "repair_python_env.py"
    result = subprocess.run(
        [str(python), "-S", str(helper), "--venv", str(environment)],
        env=env, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    for utf8 in ("0", "1"):
        result = subprocess.run(
            [str(python), "-X", f"utf8={utf8}", "-c",
             "import encoding_probe; assert encoding_probe.VALUE == 42"],
            env=env, capture_output=True,
        )
        assert result.returncode == 0, result.stderr
