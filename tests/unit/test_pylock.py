from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FIXTURES, MakeWheel, sha256
from wheelship.errors import ResolveError
from wheelship.lock import pylock
from wheelship.target import make_target

FIXTURE_LOCK = FIXTURES / "apps" / "lambda_app" / "pylock.toml"


def by_name(artifacts: list) -> dict:  # type: ignore[type-arg]
    return {a.name: a for a in artifacts}


def test_fixture_lock_linux_lambda() -> None:
    arts = by_name(pylock.select(FIXTURE_LOCK, make_target("lambda-arm64", "3.12")))
    # colorama is win32-only
    assert set(arts) == {
        "attrs", "markupsafe", "lambda-app", "awslambdaric", "simplejson", "snapshot-restore-py"
    }  # fmt: skip
    ms = arts["markupsafe"]
    assert ms.kind == "wheel"
    assert ms.filename is not None and "cp312-cp312" in ms.filename
    assert "aarch64" in ms.filename and "musllinux" not in ms.filename
    assert ms.hashes["sha256"]
    app = arts["lambda-app"]
    assert app.kind == "directory"
    assert app.path == FIXTURE_LOCK.parent.resolve()


def test_fixture_lock_cross_windows() -> None:
    arts = by_name(pylock.select(FIXTURE_LOCK, make_target("windows-x86_64", "3.13")))
    assert "colorama" in arts
    assert arts["markupsafe"].filename.endswith("cp313-cp313-win_amd64.whl")  # type: ignore[union-attr]


def test_requires_python_enforced(tmp_path: Path) -> None:
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        'lock-version = "1.0"\ncreated-by = "t"\nrequires-python = ">=3.12"\npackages = []\n'
    )
    with pytest.raises(ResolveError, match="Python version requirement"):
        pylock.select(lock, make_target("lambda-x86_64", "3.11"))


def test_local_wheels_groups_and_archives(tmp_path: Path, make_wheel: MakeWheel) -> None:
    pure = make_wheel("pure", "1.0", out=tmp_path / "w")
    win = make_wheel("native", "2.0", tag="cp312-cp312-win_amd64", out=tmp_path / "w")
    lin = make_wheel("native", "2.0", tag="cp312-cp312-manylinux_2_28_x86_64", out=tmp_path / "w")
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        f"""
lock-version = "1.0"
created-by = "test"
default-groups = ["main"]
dependency-groups = ["main", "dev"]

[[packages]]
name = "pure"
version = "1.0"
marker = "'main' in dependency_groups"
archive = {{ path = "w/{pure.name}", hashes = {{ sha256 = "{sha256(pure)}" }} }}

[[packages]]
name = "native"
version = "2.0"
wheels = [
  {{ path = "w/{win.name}", hashes = {{ sha256 = "{sha256(win)}" }} }},
  {{ path = "w/{lin.name}", hashes = {{ sha256 = "{sha256(lin)}" }} }},
]

[[packages]]
name = "devtool"
version = "1.0"
marker = "'dev' in dependency_groups"
sdist = {{ url = "https://example.invalid/devtool-1.0.tar.gz", hashes = {{ sha256 = "00" }} }}
"""
    )
    target = make_target("linux-x86_64", "3.12", glibc="2.28")
    arts = by_name(pylock.select(lock, target))
    assert set(arts) == {"pure", "native"}
    assert arts["pure"].kind == "wheel"  # archive pointing at a .whl
    assert arts["pure"].path == pure.resolve()
    assert arts["native"].path == lin.resolve()

    arts = by_name(pylock.select(lock, target, groups=["dev"]))
    assert set(arts) == {"native", "devtool"}
    assert arts["devtool"].kind == "sdist"

    with pytest.raises(ResolveError, match="No wheel found"):
        pylock.select(lock, make_target("linux-x86_64", "3.12", glibc="2.17"))


def test_invalid_lock(tmp_path: Path) -> None:
    lock = tmp_path / "pylock.toml"
    lock.write_text('lock-version = "1.0"\n')
    with pytest.raises(ResolveError, match="invalid lock file"):
        pylock.select(lock, make_target("lambda-x86_64", "3.12"))
    with pytest.raises(ResolveError, match="not found"):
        pylock.select(tmp_path / "missing.toml", make_target("lambda-x86_64", "3.12"))
