"""End-to-end: a uv workspace member built from the workspace's uv.lock (no pylock.toml)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from wheelship import launchers
from wheelship.cli import main
from wheelship.errors import BuildError
from wheelship.target import make_target

MEMBER = FIXTURES / "apps" / "uv_workspace" / "packages" / "app"
pytestmark = pytest.mark.network


def test_member_with_sibling(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("needs uv on PATH")
    try:
        launchers.find(make_target("linux-x86_64", "3.12"), "console")
    except BuildError:
        pytest.skip("no launcher built (cargo build --release)")
    assert main(["build", "-q", "-C", str(MEMBER), "-o", str(tmp_path)]) == 0
    lib = tmp_path / "ws-app-0.1.0-linux" / "lib"
    assert sorted(p.name for p in lib.glob("*.dist-info")) == [
        "markupsafe-3.0.4.dist-info",
        "ws_app-0.1.0.dist-info",
        "ws_lib-0.1.0.dist-info",  # workspace sibling, built from its directory
    ]
    assert not list(lib.glob("installer*"))  # dev group
