"""Derive a PEP 751 lock from a uv project's ``uv.lock`` at build time.

uv projects commit ``uv.lock`` only; wheelship runs ``uv export --format pylock.toml``
for the project being built (a workspace member is selected by running uv in its
directory) and reads the result. ``uv.lock`` is never modified (``--locked``).

uv writes extras and dependency groups into the export as a fixed selection rather
than as markers, so ``[tool.wheelship]`` ``extras`` and ``groups`` become uv flags here.

Relative paths in the export depend on the uv version: uv <= 0.12.19 writes them relative
to the workspace root, uv >= 0.12.20 relative to the working directory (for stdout). The
base is therefore derived from the project's own entry, which must point at the
project directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name

from ..config import ProjectConfig
from ..errors import ResolveError

LOCK_NAME = "uv.lock"


def find(start: Path) -> Path | None:
    """The nearest ``uv.lock`` in ``start`` or its parents (a workspace keeps it at the root)."""
    for d in (start, *start.parents):
        path = d / LOCK_NAME
        if path.is_file():
            return path
    return None


def export_args(cfg: ProjectConfig) -> list[str]:
    args = ["export", "--format", "pylock.toml", "--quiet", "--no-default-groups"]
    for group in cfg.groups or ():
        args += ["--group", group]
    for extra in cfg.extras:
        args += ["--extra", extra]
    if "--frozen" not in cfg.uv_export_args:
        args.append("--locked")
    return args + list(cfg.uv_export_args)


def export(cfg: ProjectConfig, dest: Path) -> tuple[Path, Path]:
    """Write the exported lock to ``dest/pylock.toml``; return it and the base of its paths."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        raise ResolveError(
            "found uv.lock but uv is not on PATH; install uv, or export a pylock.toml "
            "yourself and set [tool.wheelship].lock"
        )
    cmd = [uv, *export_args(cfg)]
    try:
        proc = subprocess.run(cmd, cwd=cfg.root, capture_output=True, text=True, check=False)
    except OSError as e:
        raise ResolveError(f"cannot run uv: {e}") from None
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit status {proc.returncode}"
        raise ResolveError(f"`uv {' '.join(cmd[1:])}` failed:\n{detail}")
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "pylock.toml"
    path.write_text(proc.stdout, encoding="utf-8")
    try:
        data = tomllib.loads(proc.stdout)
    except tomllib.TOMLDecodeError as e:
        raise ResolveError(f"uv export produced an invalid lock: {e}") from None
    return path, path_base(cfg, data)


def path_base(cfg: ProjectConfig, data: dict[str, Any]) -> Path:
    """The directory that relative paths in the export are relative to."""
    for package in data.get("packages", []):
        directory = package.get("directory")
        if canonicalize_name(package.get("name", "")) != cfg.name or directory is None:
            continue
        if os.path.isabs(directory["path"]):
            return cfg.root
        rel = PurePosixPath(directory["path"])
        parts = [p for p in rel.parts if p != "."]
        if ".." not in parts and len(parts) < len(cfg.root.parts):
            base = cfg.root.parents[len(parts) - 1] if parts else cfg.root
            if base.joinpath(*parts) == cfg.root:
                return base
        raise ResolveError(
            f"uv exported {cfg.name} at {rel}, which is not {cfg.root}; "
            "run wheelship on the project directory"
        )
    # The project is not in the export (e.g. --no-emit-project): assume uv >= 0.12.20.
    return cfg.root
