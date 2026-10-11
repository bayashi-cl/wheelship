from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from wheelship import bundle, config
from wheelship.errors import ResolveError
from wheelship.lock import uv
from wheelship.target import make_target

WORKSPACE = FIXTURES / "apps" / "uv_workspace"
MEMBER = WORKSPACE / "packages" / "app"
LINUX = make_target("linux-x86_64", "3.12")
WINDOWS = make_target("windows-x86_64", "3.12")

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH")


def names(artifacts: list) -> set[str]:  # type: ignore[type-arg]
    return {a.name for a in artifacts}


def with_tool(cfg: config.ProjectConfig, **changes: object) -> config.ProjectConfig:
    return dataclasses.replace(cfg, **changes)  # type: ignore[arg-type]


def test_find_walks_up_to_workspace_root() -> None:
    assert uv.find(MEMBER) == WORKSPACE / "uv.lock"
    cfg = config.load(MEMBER)
    assert bundle.find_lock(cfg, None) == WORKSPACE / "uv.lock"


def test_pylock_next_to_project_wins(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "pylock.toml").write_text("")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert bundle.find_lock(config.load(tmp_path), None) == tmp_path / "pylock.toml"


def test_export_args() -> None:
    cfg = config.load(MEMBER)
    assert uv.export_args(cfg) == [
        "export", "--format", "pylock.toml", "--quiet", "--no-default-groups", "--locked"
    ]  # fmt: skip
    cfg = with_tool(
        cfg,
        extras=("fancy",),
        groups=("tools",),
        uv_export_args=("--frozen", "--no-emit-workspace"),
    )
    assert uv.export_args(cfg) == [
        "export", "--format", "pylock.toml", "--quiet", "--no-default-groups",
        "--group", "tools", "--extra", "fancy", "--frozen", "--no-emit-workspace",
    ]  # fmt: skip


def test_config_uv_export_args(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[tool.wheelship]\nuv-export-args = ["--prune", "y"]\n'
    )
    assert config.load(tmp_path).uv_export_args == ("--prune", "y")


@needs_uv
def test_workspace_member(tmp_path: Path) -> None:
    cfg = config.load(MEMBER)
    lock = bundle.find_lock(cfg, None)
    arts = {a.name: a for a in bundle.resolve(cfg, LINUX, lock, tmp_path)}
    # dev group excluded; colorama is win32-only
    assert set(arts) == {"ws-app", "ws-lib", "markupsafe"}
    assert arts["ws-app"].path == MEMBER
    assert arts["ws-lib"].kind == "directory"
    assert arts["ws-lib"].path == WORKSPACE / "packages" / "lib"
    assert arts["markupsafe"].filename is not None
    assert "cp312" in arts["markupsafe"].filename

    assert "colorama" in names(bundle.resolve(cfg, WINDOWS, lock, tmp_path / "w"))


@needs_uv
def test_workspace_extras_and_groups(tmp_path: Path) -> None:
    cfg = with_tool(config.load(MEMBER), extras=("fancy",), groups=("tools",))
    lock = bundle.find_lock(cfg, None)
    assert names(bundle.resolve(cfg, LINUX, lock, tmp_path)) == {
        "ws-app", "ws-lib", "markupsafe", "packaging", "truststore"
    }  # fmt: skip


@needs_uv
def test_uv_lock_of_another_project(tmp_path: Path) -> None:
    # A standalone project whose nearest uv.lock is someone else's workspace.
    project = tmp_path / "other"
    shutil.copytree(MEMBER, project)
    shutil.copy(WORKSPACE / "uv.lock", tmp_path / "uv.lock")
    cfg = config.load(project)
    with pytest.raises(ResolveError, match="Unable to find lockfile"):
        bundle.resolve(cfg, LINUX, bundle.find_lock(cfg, None), tmp_path / "w")


@needs_uv
def test_explicit_uv_lock_elsewhere(tmp_path: Path) -> None:
    # uv exports the member's own workspace; paths must not resolve against this copy.
    shutil.copy(WORKSPACE / "uv.lock", tmp_path / "uv.lock")
    cfg = config.load(MEMBER)
    with pytest.raises(ResolveError, match="does not belong"):
        bundle.resolve(cfg, LINUX, bundle.find_lock(cfg, tmp_path / "uv.lock"), tmp_path / "w")


def test_missing_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    cfg = config.load(MEMBER)
    with pytest.raises(ResolveError, match="uv is not on PATH"):
        bundle.resolve(cfg, LINUX, WORKSPACE / "uv.lock", tmp_path)
