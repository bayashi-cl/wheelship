"""End-to-end: single-file executables and system-interpreter bundles of the CLI fixture."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from wheelship import launchers, pbs
from wheelship.bytecode import find_interpreter
from wheelship.cli import main
from wheelship.errors import BuildError
from wheelship.target import make_target

APP = FIXTURES / "apps" / "cli_app"
STEM = "cli-app-0.2.0"
pytestmark = pytest.mark.network

HOST = pbs.host_triple()


def _launcher_available(platform: str) -> bool:
    try:
        launchers.find(make_target(platform, "3.12"), "console")
    except BuildError:
        return False
    return True


def _python312() -> str:
    python = find_interpreter(make_target("linux-x86_64", "3.12"))
    if python is None:
        pytest.skip("no CPython 3.12 on the host")
    return python


def run(exe: Path | str, *args: str, cache: Path, **env: str) -> subprocess.CompletedProcess[str]:
    cmd = [str(exe), *args]
    if str(exe).endswith(".pyz"):
        cmd = [env.pop("PYTHON"), *cmd]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "WHEELSHIP_CACHE_DIR": str(cache), **env},
        cwd=Path(exe).parent.parent,
    )


@pytest.fixture(scope="module")
def linux_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if HOST != "x86_64-unknown-linux-gnu":
        pytest.skip("runs Linux bundles: needs an x86_64 Linux host")
    if not _launcher_available("linux-x86_64"):
        pytest.skip("no launcher built (cargo build --release)")
    out = tmp_path_factory.mktemp("dist")
    args = ["build", "-q", "-C", str(APP), "-o", str(out)]
    assert main([*args, "-t", "linux", "-f", "exe"]) == 0
    assert main([*args, "-t", "linux-system"]) == 0  # dir, pyz and exe
    return out


def test_bundled_exe_extracts_once(linux_dist: Path, tmp_path: Path) -> None:
    exe = tmp_path / "somewhere" / "app.bin"  # relocated and renamed
    exe.parent.mkdir()
    shutil.copy2(linux_dist / f"{STEM}-linux.bin", exe)
    cache = tmp_path / "cache"

    proc = run(exe, "--mp", "a", cache=cache)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    [extracted] = (cache / "apps" / "cli-app").iterdir()
    assert info["argv0"] == str(exe)
    assert info["executable"] == str(extracted / "python" / "bin" / "python3.12")
    assert info["mp"] == [1, 4, 9] and info["isolated"] == 1
    assert info["scripts"] == ["cli-app", "pygmentize"]
    assert (extracted / "python" / "bin" / "python3").is_symlink()  # kept in the payload
    assert not (extracted / "cli-app").exists()  # the outer exe is the launcher

    stamp = (extracted / "wheelship.json").stat().st_mtime_ns
    assert run(exe, "--exit", "4", cache=cache).returncode == 4
    assert (extracted / "wheelship.json").stat().st_mtime_ns == stamp


def test_exe_concurrent_first_runs(linux_dist: Path, tmp_path: Path) -> None:
    exe = linux_dist / f"{STEM}-linux.bin"
    cache = tmp_path / "cache"
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: run(exe, cache=cache), range(6)))
    assert [r.returncode for r in results] == [0] * 6, [r.stderr for r in results]
    assert len(list((cache / "apps" / "cli-app").iterdir())) == 1  # no leftovers


def test_exe_entry_point_by_name(linux_dist: Path, tmp_path: Path) -> None:
    exe = tmp_path / "cli-app-gui"  # busybox style: the name selects the entry point
    shutil.copy2(linux_dist / f"{STEM}-linux.bin", exe)
    proc = run(exe, cache=tmp_path / "c", WHEELSHIP_DEBUG="1")
    assert proc.returncode == 0, proc.stderr
    assert "from cli_app.main import gui" in proc.stderr


def test_exe_is_reproducible(linux_dist: Path, tmp_path: Path) -> None:
    args = ["build", "-q", "-C", str(APP), "-t", "linux", "-f", "exe", "-o", str(tmp_path)]
    assert main(args) == 0
    name = f"{STEM}-linux.bin"
    assert (tmp_path / name).read_bytes() == (linux_dist / name).read_bytes()


@pytest.mark.parametrize("kind", ["dir", "pyz", "exe"])
def test_system_interpreter_bundles(linux_dist: Path, tmp_path: Path, kind: str) -> None:
    python = _python312()
    exe = {
        "dir": linux_dist / f"{STEM}-linux-system" / "cli-app",
        "pyz": linux_dist / f"{STEM}-linux-system.pyz",
        "exe": linux_dist / f"{STEM}-linux-system.bin",
    }[kind]
    env = {"PYTHON": python} if kind == "pyz" else {"WHEELSHIP_PYTHON": python}
    proc = run(exe, "--mp", "z", cache=tmp_path / "cache", **env)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    assert Path(info["executable"]).resolve() == Path(python).resolve()
    assert info["mp"] == [1, 4, 9]
    assert info["native_ext"] == "_speedups.cpython-312-x86_64-linux-gnu.so"
    # Only the bundle's distributions: the interpreter's site-packages stay out.
    assert info["scripts"] == ["cli-app", "pygmentize"]
    assert info["isolated"] == (0 if kind == "pyz" else 1)


def test_system_bundle_layout(linux_dist: Path) -> None:
    root = linux_dist / f"{STEM}-linux-system"
    assert sorted(p.name for p in root.iterdir()) == [
        "_wheelship_boot.py", "cli-app", "cli-app-gui", "lib", "wheelship.json",
    ]  # fmt: skip
    assert (root / "lib" / "markupsafe" / "_speedups.cpython-312-x86_64-linux-gnu.so").is_file()
    assert not list(root.glob("lib/bin")) and not (root / "python").exists()
    doc = json.loads((root / "wheelship.json").read_text())
    assert doc["interpreter"]["mode"] == "system"


def test_system_wrong_interpreter(linux_dist: Path, tmp_path: Path) -> None:
    other = find_interpreter(make_target("linux-x86_64", "3.13"))
    if other is None:
        pytest.skip("no CPython 3.13 to provoke a mismatch")
    exe = linux_dist / f"{STEM}-linux-system" / "cli-app"
    proc = run(exe, cache=tmp_path, WHEELSHIP_PYTHON=other)
    assert proc.returncode == 1
    assert "needs CPython 3.12" in proc.stderr


def test_windows_cross_build_onefile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not _launcher_available("windows-x86_64"):
        fake = tmp_path / "launchers" / "x86_64-pc-windows-msvc"
        fake.mkdir(parents=True)
        (fake / "wheelship-launcher.exe").write_bytes(b"console")
        (fake / "wheelship-launcher-gui.exe").write_bytes(b"gui")
        monkeypatch.setenv("WHEELSHIP_LAUNCHER_DIR", str(fake.parent))
    out = tmp_path / "dist"
    assert main(["build", "-q", "-C", str(APP), "-t", "windows-system", "-o", str(out)]) == 0
    with zipfile.ZipFile(out / f"{STEM}-windows-system.zip") as zf:
        names = set(zf.namelist())
    root = f"{STEM}-windows-system/"
    assert {root + "cli-app.exe", root + "_wheelship_boot.py", root + "wheelship.json"} <= names
    assert root + "lib/markupsafe/_speedups.cp312-win_amd64.pyd" in names
    pyz = out / f"{STEM}-windows-system.pyz"
    assert pyz.read_bytes().startswith(b"#!/usr/bin/env python3.12\n")
    with zipfile.ZipFile(pyz) as zf:
        assert {"__main__.py", "wheelship.json"} <= set(zf.namelist())


@pytest.mark.skipif(HOST != "x86_64-pc-windows-msvc", reason="needs a Windows host")
def test_onefile_and_system_on_windows(tmp_path: Path) -> None:
    if not _launcher_available("windows-x86_64"):
        pytest.skip("no Windows launcher available")
    out = tmp_path / "dist"
    args = ["build", "-q", "-C", str(APP), "-o", str(out)]
    assert main([*args, "-t", "windows", "-f", "exe"]) == 0
    assert main([*args, "-t", "windows-system"]) == 0
    cache = tmp_path / "cache"

    proc = run(out / f"{STEM}-windows.exe", "--mp", "a", cache=cache)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    assert info["executable"].lower().startswith(str(cache).lower())
    assert info["mp"] == [1, 4, 9]
    assert run(out / f"{STEM}-windows.exe", "--exit", "6", cache=cache).returncode == 6

    python = _python312()
    with zipfile.ZipFile(out / f"{STEM}-windows-system.zip") as zf:
        zf.extractall(tmp_path / "x")
    exe = tmp_path / "x" / f"{STEM}-windows-system" / "cli-app.exe"
    proc = run(exe, "--mp", cache=cache, WHEELSHIP_PYTHON=python)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["mp"] == [1, 4, 9]

    # gui_scripts: GUI-subsystem launcher + pythonw.exe (no console, no stdout).
    assert main([*args, "-t", "windows", "-f", "archive"]) == 0
    with zipfile.ZipFile(out / f"{STEM}-windows.zip") as zf:
        zf.extractall(tmp_path / "b")
    report = tmp_path / "gui.json"
    gui = tmp_path / "b" / f"{STEM}-windows" / "cli-app-gui.exe"
    proc = run(gui, "--write", str(report), "--exit", "9", cache=cache)
    assert proc.returncode == 9, proc.stderr
    assert json.loads(report.read_text())["executable"].lower().endswith("pythonw.exe")

    proc = run(out / f"{STEM}-windows-system.pyz", cache=cache, PYTHON=python)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["native_ext"] == "_speedups.cp312-win_amd64.pyd"
