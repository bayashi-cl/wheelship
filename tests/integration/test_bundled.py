"""End-to-end: bundled (python-build-standalone) builds of the CLI fixture app."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from wheelship import launchers, pbs
from wheelship.cli import main
from wheelship.errors import BuildError
from wheelship.target import make_target

APP = FIXTURES / "apps" / "cli_app"
STEM = "cli-app-0.2.0"
pytestmark = pytest.mark.network

HOST_LINUX_X86 = pbs.host_triple() == "x86_64-unknown-linux-gnu"


def _has_launcher(platform: str) -> bool:
    try:
        launchers.find(make_target(platform, "3.12"), "console")
    except BuildError:
        return False
    return True


@pytest.fixture(scope="module")
def linux_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HOST_LINUX_X86:
        pytest.skip("runs the bundle: needs an x86_64 Linux host")
    if not _has_launcher("linux-x86_64"):
        pytest.skip("no launcher built (cargo build --release)")
    out = tmp_path_factory.mktemp("dist")
    assert main(["build", "-q", "-C", str(APP), "-t", "linux", "-o", str(out)]) == 0
    return out


def run(exe: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(exe), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        cwd=exe.parent.parent,  # anywhere but the bundle
    )


def test_layout(linux_dist: Path) -> None:
    bundle = linux_dist / f"{STEM}-linux"
    assert sorted(p.name for p in bundle.iterdir()) == [
        "cli-app",
        "cli-app-gui",
        "python",
        "wheelship.json",
    ]
    py = bundle / "python"
    assert (py / "bin" / "python3.12").is_file()
    assert not list(py.glob("lib/python3.12/site-packages/pip*"))
    assert not (py / "lib" / "python3.12" / "ensurepip").exists()
    assert not (py / "include").exists()  # "dev" strip group
    manifest = json.loads((bundle / "wheelship.json").read_text())
    assert manifest["interpreter"]["path"] == "python/bin/python3.12"
    assert manifest["default_entry_point"] == "cli-app"


def test_runs_relocated(linux_dist: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "somewhere else" / "app"  # space in the path on purpose
    shutil.copytree(linux_dist / f"{STEM}-linux", bundle, symlinks=True)
    proc = run(bundle / "cli-app", "--mp", "a", "b c")
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    assert info["prog"] == "cli-app"
    assert info["argv0"] == str(bundle / "cli-app")
    assert info["rest"] == ["a", "b c"]
    assert info["executable"] == str(bundle / "python" / "bin" / "python3.12")
    assert info["isolated"] == 1 and info["cwd_in_path"] is False
    assert info["native_ext"] == "_speedups.cpython-312-x86_64-linux-gnu.so"
    assert info["scripts"] == ["cli-app", "pygmentize"]
    assert info["version"].startswith("3.12.")
    assert info["mp"] == [1, 4, 9]

    assert run(bundle / "cli-app", "--exit", "7").returncode == 7

    # A dependency's console script is relocatable too.
    proc = run(bundle / "python" / "bin" / "pygmentize", "-V")
    assert proc.returncode == 0 and "Pygments version" in proc.stdout

    # Symlinking the launcher elsewhere (e.g. into ~/.local/bin) still works.
    link = tmp_path / "cli-app"
    link.symlink_to(bundle / "cli-app")
    assert json.loads(run(link).stdout)["executable"].startswith(str(bundle))


def test_boot_and_debug(linux_dist: Path) -> None:
    exe = linux_dist / f"{STEM}-linux" / "cli-app"
    proc = run(exe, "-c", "import sys; print(sys.prefix)", WHEELSHIP_BOOT="python")
    assert proc.stdout.strip() == str(exe.parent / "python")
    proc = run(exe, WHEELSHIP_DEBUG="1")
    assert "wheelship-launcher: exec" in proc.stderr


def test_launcher_errors(linux_dist: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "b"
    shutil.copytree(linux_dist / f"{STEM}-linux", bundle, symlinks=True)
    (bundle / "wheelship.json").write_text('{"schema": 99}')
    proc = run(bundle / "cli-app")
    assert proc.returncode == 1 and "wheelship-launcher:" in proc.stderr


def test_archive_reproducible_and_complete(linux_dist: Path, tmp_path: Path) -> None:
    archive = linux_dist / f"{STEM}-linux.tar.gz"
    with tarfile.open(archive) as tf:
        names = tf.getnames()
        link = tf.getmember(f"{STEM}-linux/python/bin/python3")
        launcher = tf.getmember(f"{STEM}-linux/cli-app")
    assert link.issym() and link.linkname == "python3.12"
    assert launcher.mode == 0o755
    assert f"{STEM}-linux/wheelship.json" in names
    assert (
        main(["build", "-q", "-C", str(APP), "-t", "linux", "-f", "archive", "-o", str(tmp_path)])
        == 0
    )
    assert (tmp_path / archive.name).read_bytes() == archive.read_bytes()


def test_windows_cross_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assemble a Windows bundle on any host; fake launchers stand in when none is built."""
    if not _has_launcher("windows-x86_64"):
        fake = tmp_path / "launchers" / "x86_64-pc-windows-msvc"
        fake.mkdir(parents=True)
        (fake / "wheelship-launcher.exe").write_bytes(b"console")
        (fake / "wheelship-launcher-gui.exe").write_bytes(b"gui")
        monkeypatch.setenv("WHEELSHIP_LAUNCHER_DIR", str(fake.parent))
    out = tmp_path / "dist"
    assert main(["build", "-q", "-C", str(APP), "-t", "windows", "-o", str(out)]) == 0
    with zipfile.ZipFile(out / f"{STEM}-windows.zip") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(f"{STEM}-windows/wheelship.json"))
    root = f"{STEM}-windows/"
    assert {root + "cli-app.exe", root + "cli-app-gui.exe", root + "python/python.exe"} <= names
    site = root + "python/Lib/site-packages/"
    assert site + "markupsafe/_speedups.cp312-win_amd64.pyd" in names
    assert any(n.startswith(site + "colorama/") for n in names)  # win32-only dependency
    assert root + "python/Scripts/pygmentize.cmd" in names
    assert not any(n.startswith(site + "pip") for n in names)
    assert site + "pygments/__pycache__/__init__.cpython-312.pyc" in names
    assert manifest["interpreter"]["gui_path"] == "python/pythonw.exe"
    assert manifest["entry_points"]["cli-app-gui"]["gui"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="checks the lock file written on Linux CI too")
def test_pbs_lock_is_pinned() -> None:
    lock = pbs.Lock.load(APP / "wheelship.lock")
    assert lock.release and all(len(sha) == 64 for sha in lock.assets.values())


@pytest.mark.skipif(pbs.host_triple() != "x86_64-pc-windows-msvc", reason="needs a Windows host")
def test_runs_on_windows(tmp_path: Path) -> None:
    if not _has_launcher("windows-x86_64"):
        pytest.skip("no Windows launcher available")
    out = tmp_path / "dist"
    assert main(["build", "-q", "-C", str(APP), "-t", "windows", "-o", str(out)]) == 0
    with zipfile.ZipFile(out / f"{STEM}-windows.zip") as zf:
        zf.extractall(tmp_path / "x")
    bundle = tmp_path / "x" / f"{STEM}-windows"
    proc = run(bundle / "cli-app.exe", "--mp", "a")
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    assert info["executable"].lower() == str(bundle / "python" / "python.exe").lower()
    assert info["native_ext"] == "_speedups.cp312-win_amd64.pyd"
    assert info["mp"] == [1, 4, 9]
    assert run(bundle / "cli-app.exe", "--exit", "7").returncode == 7
    proc = subprocess.run(
        ["cmd", "/c", str(bundle / "python" / "Scripts" / "pygmentize.cmd"), "-V"],
        capture_output=True,
        text=True,
    )
    assert "Pygments version" in proc.stdout, proc.stderr
