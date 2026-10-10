"""Phase 1 building blocks: PBS resolution, trimming, launchers, manifest, scripts, tar."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.conftest import MakeWheel
from wheelship import launchers, manifest, pbs, reproducible
from wheelship.errors import BuildError, ConfigError
from wheelship.install import flat_scheme, install_wheels
from wheelship.lock import Artifact
from wheelship.target import make_target
from wheelship.wheels import Wheel, entry_points

R = "20261009"
SUMS = {
    f"cpython-3.12.14+{R}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz": "a" * 64,
    f"cpython-3.12.15+{R}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz": "b" * 64,
    f"cpython-3.12.15+{R}-x86_64-unknown-linux-gnu-install_only.tar.gz": "c" * 64,
    f"cpython-3.12.15+{R}-x86_64_v3-unknown-linux-gnu-install_only_stripped.tar.gz": "d" * 64,
    f"cpython-3.12.15+{R}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz": "e" * 64,
    f"cpython-3.13.9+{R}-x86_64-unknown-linux-gnu-freethreaded-install_only.tar.gz": "f" * 64,
    f"cpython-3.15.0rc1+{R}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz": "0" * 64,
    f"cpython-3.12.15+{R}-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst": "1" * 64,
}
LINUX = "x86_64-unknown-linux-gnu"
STRIPPED = "install_only_stripped"


def test_select_asset_newest_patch_and_exact() -> None:
    newest = pbs.select_asset(SUMS, R, (3, 12), LINUX, STRIPPED)
    assert newest is not None and newest.version == (3, 12, 15) and newest.sha256 == "b" * 64
    exact = pbs.select_asset(SUMS, R, (3, 12, 14), LINUX, STRIPPED)
    assert exact is not None and exact.sha256 == "a" * 64
    full = pbs.select_asset(SUMS, R, (3, 12), LINUX, "install_only")
    assert full is not None and full.sha256 == "c" * 64
    win = pbs.select_asset(SUMS, R, (3, 12), "x86_64-pc-windows-msvc", STRIPPED)
    assert win is not None and win.name.endswith("windows-msvc-install_only_stripped.tar.gz")
    # free-threaded builds and pre-releases are never picked
    assert pbs.select_asset(SUMS, R, (3, 13), LINUX, "install_only") is None
    assert pbs.select_asset(SUMS, R, (3, 15), LINUX, STRIPPED) is None
    assert pbs.select_asset(SUMS, "20200101", (3, 12), LINUX, STRIPPED) is None


def test_asset_url_honors_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    asset = pbs.select_asset(SUMS, R, (3, 12), LINUX, STRIPPED)
    assert asset is not None
    assert asset.url.startswith(
        f"https://github.com/astral-sh/python-build-standalone/releases/download/{R}/"
    )
    monkeypatch.setenv("WHEELSHIP_PBS_MIRROR", "https://mirror.example/pbs/")
    assert asset.url == f"https://mirror.example/pbs/{R}/{asset.name}"


def test_resolve_pins_and_reuses_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pbs, "latest_release", lambda: calls.append("latest") or R)
    monkeypatch.setattr(pbs, "release_sums", lambda r: calls.append(f"sums:{r}") or SUMS)
    lock = tmp_path / "wheelship.lock"

    asset = pbs.resolve(pbs.PbsConfig(), (3, 12), LINUX, lock)
    assert asset.version == (3, 12, 15)
    assert calls == ["latest", f"sums:{R}"]
    text = lock.read_text()
    assert f'release = "{R}"' in text and asset.name in text and "b" * 64 in text

    calls.clear()
    again = pbs.resolve(pbs.PbsConfig(), (3, 12), LINUX, lock)
    assert again == asset
    assert calls == []  # fully served from wheelship.lock: no network

    # A different target adds a second pinned asset to the same release.
    pbs.resolve(pbs.PbsConfig(), (3, 12), "x86_64-pc-windows-msvc", lock)
    assert pbs.Lock.load(lock).assets.keys() == {
        asset.name,
        f"cpython-3.12.15+{R}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
    }

    # Changing the configured release starts a fresh pin set.
    monkeypatch.setattr(pbs, "release_sums", lambda r: {})
    with pytest.raises(ConfigError, match="has no CPython 3.12"):
        pbs.resolve(pbs.PbsConfig(release="20200101"), (3, 12), LINUX, lock)


def test_lock_roundtrip(tmp_path: Path) -> None:
    lock = pbs.Lock(R, {"b.tar.gz": "2" * 64, "a.tar.gz": "1" * 64})
    path = tmp_path / "wheelship.lock"
    lock.save(path)
    assert pbs.Lock.load(path) == lock
    assert path.read_text().index("a.tar.gz") < path.read_text().index("b.tar.gz")
    assert pbs.Lock.load(tmp_path / "missing") == pbs.Lock()


def _touch(root: Path, *paths: str) -> None:
    for rel in paths:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")


def test_trim_linux(tmp_path: Path) -> None:
    target = make_target("linux-x86_64", "3.12")
    stdlib = "lib/python3.12"
    _touch(
        tmp_path,
        "bin/python3.12", "bin/pip3", "bin/2to3-3.12", "bin/python3.12-config",
        f"{stdlib}/os.py", f"{stdlib}/ensurepip/__init__.py",
        f"{stdlib}/site-packages/pip/__init__.py",
        f"{stdlib}/site-packages/pip-26.0.dist-info/RECORD",
        f"{stdlib}/site-packages/README.txt",
        f"{stdlib}/test/test_os.py", f"{stdlib}/idlelib/idle.py", f"{stdlib}/tkinter/__init__.py",
        "include/python3.12/Python.h", "lib/libpython3.12.so.1.0",
    )  # fmt: skip
    pbs.trim(tmp_path, target, ["dev", "test"])
    left = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert left == [
        "bin/python3.12",
        f"{stdlib}/idlelib/idle.py",
        f"{stdlib}/os.py",
        f"{stdlib}/site-packages/README.txt",
        f"{stdlib}/tkinter/__init__.py",
    ]
    with pytest.raises(ConfigError, match="unknown strip group"):
        pbs.strip_patterns(target, ["docs"])


def test_trim_windows_patterns() -> None:
    patterns = pbs.strip_patterns(make_target("windows-x86_64", "3.12"), ["tkinter"])
    assert "Lib/site-packages/pip/**" in patterns and "Scripts/**" in patterns
    assert "DLLs/_tkinter.pyd" in patterns and "tcl/**" in patterns


def test_layout() -> None:
    win = pbs.Layout.for_target(make_target("windows-x86_64", "3.12"))
    assert (win.interpreter, win.gui_interpreter, win.site_packages) == (
        "python.exe", "pythonw.exe", "Lib/site-packages"
    )  # fmt: skip
    lin = pbs.Layout.for_target(make_target("linux-aarch64", "3.13"))
    assert (lin.interpreter, lin.site_packages) == (
        "bin/python3.13", "lib/python3.13/site-packages"
    )  # fmt: skip


def test_launcher_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launchers, "_PACKAGE_DIR", tmp_path / "pkg")
    monkeypatch.setattr(launchers, "_repo_target_dir", lambda: None)
    monkeypatch.delenv("WHEELSHIP_LAUNCHER_DIR", raising=False)
    win = make_target("windows-x86_64", "3.12")
    with pytest.raises(BuildError, match="no gui launcher for x86_64-pc-windows-msvc"):
        launchers.find(win, "gui")

    packaged = tmp_path / "pkg" / "x86_64-pc-windows-msvc" / "wheelship-launcher-gui.exe"
    packaged.parent.mkdir(parents=True)
    packaged.write_text("")
    assert launchers.find(win, "gui") == packaged

    override = tmp_path / "env" / "x86_64-pc-windows-msvc" / "wheelship-launcher-gui.exe"
    override.parent.mkdir(parents=True)
    override.write_text("")
    monkeypatch.setenv("WHEELSHIP_LAUNCHER_DIR", str(tmp_path / "env"))
    assert launchers.find(win, "gui") == override
    assert launchers.binary_name(make_target("linux-x86_64", "3.12"), "gui") == "wheelship-launcher"


def test_entry_code() -> None:
    assert manifest.entry_code("a.b", "c.d", pop_argv0=True) == (
        "import sys; sys.argv.pop(0); from a.b import c; sys.exit(c.d())"
    )
    assert "pop" not in manifest.entry_code("a", "b", pop_argv0=False)
    with pytest.raises(ValueError):
        manifest.entry_code("a; import os", "b", pop_argv0=False)


def test_manifest_document() -> None:
    eps = [
        manifest.EntryPoint("tool-gui", "tool.app", "gui", "gui"),
        manifest.EntryPoint("tool-cli", "tool.app", "main", "console"),
    ]
    doc = manifest.build(
        app_name="tool",
        app_version="1.0",
        target=make_target("windows-x86_64", "3.12.15"),
        interpreter="python/python.exe",
        gui_interpreter="python/pythonw.exe",
        python_flags=["-I"],
        entry_points=eps,
        wheelship_version="0",
    )
    assert doc["schema"] == 1
    assert doc["default_entry_point"] == "tool-cli"  # no script named like the app: first console
    assert doc["entry_points"]["tool-gui"]["gui"] is True
    assert doc["entry_points"]["tool-cli"]["args"][:2] == ["-I", "-c"]
    assert doc["interpreter"]["version"] == "3.12.15"


def _tool_wheel(make_wheel: MakeWheel) -> Path:
    return make_wheel(
        "tool",
        files={"tool/__init__.py": "import sys\ndef main():\n    print('ok', sys.argv[1:])\n"},
        entry_points="[console_scripts]\ntool = tool:main\n[gui_scripts]\ntool-gui = tool:main\n",
    )


def test_entry_points_from_wheel(make_wheel: MakeWheel) -> None:
    eps = entry_points(_tool_wheel(make_wheel))
    assert [(e.name, e.module, e.attr, e.section) for e in eps] == [
        ("tool", "tool", "main", "console"),
        ("tool-gui", "tool", "main", "gui"),
    ]
    assert entry_points(make_wheel("plain")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX sh script")
def test_relocatable_posix_script_runs(tmp_path: Path, make_wheel: MakeWheel) -> None:
    w = _tool_wheel(make_wheel)
    prefix = tmp_path / "py"
    site = prefix / "site"
    target = make_target("linux-x86_64", "3.12")
    install_wheels(
        [Wheel(Artifact("tool", "1.0", "wheel"), w)],  # type: ignore[arg-type]
        flat_scheme(prefix, site),
        target,
        interpreter="python-for-test",
        relocatable=True,
    )
    moved = tmp_path / "moved"
    os.rename(prefix, moved)  # scripts must survive relocation
    # Stand-in interpreter next to the script, seeing the install via PYTHONPATH.
    wrapper = moved / "bin" / "python-for-test"
    wrapper.write_text(f'#!/bin/sh\nPYTHONPATH="{moved / "site"}" exec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    out = subprocess.run(
        [moved / "bin" / "tool", "x", "y z"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "ok ['x', 'y z']"


def test_relocatable_windows_script(tmp_path: Path, make_wheel: MakeWheel) -> None:
    w = _tool_wheel(make_wheel)
    install_wheels(
        [Wheel(Artifact("tool", "1.0", "wheel"), w)],  # type: ignore[arg-type]
        flat_scheme(tmp_path, tmp_path / "Lib"),
        make_target("windows-x86_64", "3.12"),
        interpreter="../python.exe",
        relocatable=True,
        python_flags=["-I"],
    )
    text = (tmp_path / "bin" / "tool.cmd").read_bytes().decode()
    assert text.startswith("@echo off\r\n")
    assert (
        '"%~dp0..\\python.exe" -I -c "import sys; sys.argv.pop(0); from tool import main;' in text
    )
    assert '"%~f0" %*' in text
    assert not (tmp_path / "bin" / "tool.exe").exists()


def test_tar_gz_deterministic_with_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "python" / "bin").mkdir(parents=True)
    (root / "python" / "bin" / "python3.12").write_text("elf")
    (root / "python" / "bin" / "python3").symlink_to("python3.12")
    (root / "app").write_text("launcher")
    (root / "wheelship.json").write_text("{}")
    a, b = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"

    def exe(rel: str) -> bool:
        return rel == "app" or rel.startswith("python/bin/")

    reproducible.write_tar_gz(root, a, prefix="bundle", executable=exe)
    (root / "app").touch()
    reproducible.write_tar_gz(root, b, prefix="bundle", executable=exe)
    assert a.read_bytes() == b.read_bytes()
    with tarfile.open(a) as tf:
        members = {m.name: m for m in tf.getmembers()}
    assert list(members) == [
        "bundle", "bundle/app", "bundle/wheelship.json", "bundle/python",
        "bundle/python/bin", "bundle/python/bin/python3", "bundle/python/bin/python3.12",
    ]  # fmt: skip
    link = members["bundle/python/bin/python3"]
    assert link.issym() and link.linkname == "python3.12"
    assert members["bundle/app"].mode == 0o755
    assert members["bundle/wheelship.json"].mode == 0o644
    assert {m.uid for m in members.values()} == {0} and {m.mtime for m in members.values()} == {
        reproducible.source_date_epoch()
    }
