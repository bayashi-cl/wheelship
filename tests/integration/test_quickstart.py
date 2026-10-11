"""docs/quickstart.md, followed step by step.

The application code and the ``[tool.wheelship]`` snippet are taken from the
page itself, so editing them there is covered by this test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.conftest import REPO
from wheelship import launchers, pbs
from wheelship.cli import main
from wheelship.errors import BuildError
from wheelship.target import make_target

PAGE = REPO / "docs" / "quickstart.md"
STEM = "hello-0.1.0-linux"
pytestmark = pytest.mark.network


def _blocks(lang: str) -> list[str]:
    return re.findall(rf"^```{lang}\n(.*?)^```", PAGE.read_text(encoding="utf-8"), re.M | re.S)


def _block(lang: str, needle: str) -> str:
    (found,) = [b for b in _blocks(lang) if needle in b]
    return found


def _uv(*args: str, cwd: Path) -> None:
    subprocess.run(["uv", *args], cwd=cwd, check=True)


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if pbs.host_triple() != "x86_64-unknown-linux-gnu":
        pytest.skip("the Quick Start targets x86_64 Linux")
    if shutil.which("uv") is None:
        pytest.skip("needs uv on PATH")
    try:
        launchers.find(make_target("linux-x86_64", "3.13"), "console")
    except BuildError:
        pytest.skip("no launcher built (cargo build --release)")

    root = tmp_path_factory.mktemp("quickstart")
    _uv("init", "--package", "--python", "3.13", "--vcs", "none", "hello", cwd=root)
    project = root / "hello"
    _uv("add", "rich", cwd=project)
    (project / "src" / "hello" / "__init__.py").write_text(_block("python", "def main"))
    with (project / "pyproject.toml").open("a") as f:
        f.write("\n" + _block("toml", "[tool.wheelship]"))

    assert main(["build", "-q", "-C", str(project)]) == 0
    assert (project / "wheelship.lock").is_file()
    return project / "dist"


def test_outputs(dist: Path) -> None:
    assert sorted(p.name for p in dist.iterdir()) == [
        f"{STEM}.bin",
        f"{STEM}.cdx.json",
        f"{STEM}.tar.gz",
    ]


def test_run_exe(dist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHEELSHIP_CACHE_DIR", str(tmp_path / "cache"))
    out = subprocess.run(
        [str(dist / f"{STEM}.bin")], capture_output=True, text=True, check=True
    ).stdout
    assert out.startswith("Hello from Python 3.13.")


def test_run_archive(dist: Path, tmp_path: Path) -> None:
    with tarfile.open(dist / f"{STEM}.tar.gz") as tf:
        tf.extractall(tmp_path, filter="tar")
    exe = tmp_path / STEM / "hello"
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
    assert out.startswith("Hello from Python 3.13.")
    assert str(tmp_path / STEM / "python") in out.replace("\n", "")
