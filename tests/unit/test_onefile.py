"""Phase 2 building blocks: payload zips, system-mode manifest and the boot script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from wheelship import config, manifest, reproducible
from wheelship.errors import ConfigError
from wheelship.formats.system import boot_script
from wheelship.target import make_target


def test_zip_symlinks_and_exclude(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python3.12").write_text("elf")
    (root / "bin" / "python3").symlink_to("python3.12")
    (root / "launcher").write_text("stub")
    out = tmp_path / "p.zip"
    size = reproducible.write_zip(root, out, symlinks=True, exclude=lambda r: r == "launcher")
    assert size == 3
    with zipfile.ZipFile(out) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        assert list(infos) == ["bin/python3", "bin/python3.12"]
        link = infos["bin/python3"]
        assert link.external_attr >> 16 == 0o120777
        assert zf.read(link) == b"python3.12"
    # Without symlinks=True links are dereferenced (the Lambda/Windows behavior).
    reproducible.write_zip(root, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.read("bin/python3") == b"elf"


def test_tree_digest(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("1")
    first = reproducible.tree_digest(tmp_path)
    assert first == reproducible.tree_digest(tmp_path)
    (tmp_path / "a" / "x.py").write_text("2")
    assert reproducible.tree_digest(tmp_path) != first


def _system_doc(platform: str) -> dict[str, Any]:
    return manifest.build(
        app_name="tool",
        app_version="1",
        target=make_target(platform, "3.12"),
        mode="system",
        python_flags=["-I"],
        entry_points=[manifest.EntryPoint("tool", "tool", "main", "console")],
        wheelship_version="0",
    )


def test_system_manifest() -> None:
    linux = _system_doc("linux-x86_64")
    assert linux["interpreter"]["mode"] == "system"
    assert linux["interpreter"]["python"] == "3.12"
    assert [c["program"] for c in linux["interpreter"]["candidates"]] == ["python3.12", "python3"]
    assert linux["entry_points"]["tool"]["args"] == [
        "-I",
        "-S",
        "{root}/_wheelship_boot.py",
        "tool",
    ]
    win = _system_doc("windows-x86_64")
    assert win["interpreter"]["candidates"][0] == {
        "program": "py", "gui_program": "pyw", "args": ["-3.12"]
    }  # fmt: skip
    assert "path" not in win["interpreter"]


@pytest.mark.parametrize(
    ("platform", "interpreter", "fmt", "message"),
    [
        ("linux-x86_64", "bundled", "pyz", "set interpreter = 'system'"),
        ("lambda-x86_64", "system", "exe", "not for Lambda"),
        ("lambda-x86_64", "system", "pyz", "not for Lambda"),
    ],
)
def test_format_rules(platform: str, interpreter: str, fmt: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        config.make_target_config("t", platform, "3.12", interpreter=interpreter, formats=[fmt])


def test_system_formats_allowed() -> None:
    tc = config.make_target_config(
        "t",
        "windows-x86_64",
        "3.12",
        interpreter="system",
        formats=["dir", "archive", "exe", "pyz"],
    )
    assert tc.formats == ("dir", "archive", "exe", "pyz")


# -- the boot script, run for real with this interpreter -------------------------


def _bundle(root: Path, *, python: str | None = None, payload: bool = False) -> dict[str, Any]:
    """A minimal system-mode bundle for the running interpreter."""
    lib = root / "lib"
    (lib / "tool").mkdir(parents=True)
    (lib / "tool" / "__init__.py").write_text(
        "import json, sys\n"
        "def main():\n"
        "    print(json.dumps({'argv': sys.argv, 'path': sys.path}))\n"
        "    return 3\n"
    )
    # A .pth file must be honored (site.addsitedir).
    (lib / "extra.pth").write_text("extra_dir\n")
    (lib / "extra_dir").mkdir()
    doc = {
        "schema": 1,
        "app": {"name": "tool", "version": "1"},
        "interpreter": {
            "mode": "system",
            "python": python or "{}.{}".format(*sys.version_info[:2]),
            "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
        },
        "entry_points": {"tool": {"args": [], "value": "tool:main"}},
        "default_entry_point": "tool",
    }
    if payload:
        doc["payload"] = {"sha256": reproducible.tree_digest(lib)}
    (root / "wheelship.json").write_text(json.dumps(doc))
    return doc


def _run(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, env={**os.environ, **(env or {})}
    )


def test_boot_dir_mode(tmp_path: Path) -> None:
    _bundle(tmp_path)
    boot = tmp_path / "_wheelship_boot.py"
    boot.write_bytes(boot_script())
    proc = _run(sys.executable, "-I", "-S", boot, "tool", "/opt/tool/tool", "a", "b c")
    assert proc.returncode == 3, proc.stderr
    out = json.loads(proc.stdout)
    assert out["argv"] == ["/opt/tool/tool", "a", "b c"]
    assert str(tmp_path / "lib") in out["path"]
    assert str(tmp_path / "lib" / "extra_dir") in out["path"]
    assert not any(p.endswith("site-packages") for p in out["path"])


def test_boot_rejects_other_python(tmp_path: Path) -> None:
    _bundle(tmp_path, python="2.7")
    boot = tmp_path / "_wheelship_boot.py"
    boot.write_bytes(boot_script())
    proc = _run(sys.executable, boot, "tool", "x")
    assert proc.returncode == 1
    assert "tool needs CPython 2.7" in proc.stderr and "WHEELSHIP_PYTHON" in proc.stderr


def test_boot_zipapp_extracts_once(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    doc = _bundle(stage, payload=True)
    (stage / "__main__.py").write_bytes(boot_script())
    pyz = tmp_path / "tool.pyz"
    reproducible.write_zip(stage, pyz)
    cache = tmp_path / "cache"
    env = {"WHEELSHIP_CACHE_DIR": str(cache)}

    proc = _run(sys.executable, pyz, "x", env=env)
    assert proc.returncode == 3, proc.stderr
    out = json.loads(proc.stdout)
    extracted = cache / "apps" / "tool" / doc["payload"]["sha256"][:16]
    assert out["argv"] == [str(pyz), "x"]
    assert str(extracted / "lib") in out["path"]
    # The interpreter's own site-packages are dropped even without -S.
    assert not any(p.endswith("site-packages") for p in out["path"])
    marker = extracted / "lib" / "tool" / "__init__.py"
    before = marker.stat().st_mtime_ns
    assert _run(sys.executable, pyz, env=env).returncode == 3
    assert marker.stat().st_mtime_ns == before  # reused, not re-extracted
    assert [p.name for p in (cache / "apps" / "tool").iterdir()] == [extracted.name]


def test_boot_requires_launcher_arguments(tmp_path: Path) -> None:
    _bundle(tmp_path)
    boot = tmp_path / "_wheelship_boot.py"
    boot.write_bytes(boot_script())
    proc = _run(sys.executable, boot)
    assert proc.returncode == 1 and "through its launcher" in proc.stderr


def test_fixture_targets_parse() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "apps" / "cli_app"
    cfg = config.parse(tomllib.loads((root / "pyproject.toml").read_text()), root)
    assert cfg.target("linux-system").interpreter == "system"
    assert cfg.target("windows-system").formats == ("archive", "pyz")
