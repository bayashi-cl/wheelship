"""Derive a PEP 751 lock from a uv project's ``uv.lock`` at build time.

uv projects commit ``uv.lock`` only; wheelship runs ``uv export --format pylock.toml``
for the project being built (a workspace member is selected by running uv in its
directory) and reads the result. ``uv.lock`` is never modified (``--locked``).

uv writes extras and dependency groups into the export as a fixed selection rather
than as markers, so ``[tool.wheelship]`` ``extras`` and ``groups`` become uv flags here.
Paths in the export are relative to the workspace root, i.e. the ``uv.lock`` directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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


def export(cfg: ProjectConfig, dest: Path) -> Path:
    """Write the exported lock to ``dest/pylock.toml`` and return its path."""
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
    return path
