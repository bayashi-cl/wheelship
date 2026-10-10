"""Locate prebuilt launcher binaries.

Released pybox wheels carry launchers for every supported target under
``pybox/_launchers/<triple>/``, so users never need a Rust toolchain. During
development the launchers come from this repository's cargo ``target/``
directory, or from ``PYBOX_LAUNCHER_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from .errors import BuildError
from .pbs import host_triple, target_triple
from .target import Target

Kind = Literal["console", "gui"]
_PACKAGE_DIR = Path(__file__).resolve().parent / "_launchers"


def binary_name(target: Target, kind: Kind) -> str:
    if target.os == "windows":
        return "pybox-launcher-gui.exe" if kind == "gui" else "pybox-launcher.exe"
    # There is no GUI subsystem on Linux: both kinds use the console launcher.
    return "pybox-launcher"


def _repo_target_dir() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "Cargo.toml").is_file() and (parent / "crates" / "pybox-launcher").is_dir():
            return parent / "target"
    return None


def candidates(target: Target, kind: Kind) -> list[Path]:
    name = binary_name(target, kind)
    triple = target_triple(target)
    result: list[Path] = []
    if env := os.environ.get("PYBOX_LAUNCHER_DIR"):
        result += [Path(env) / triple / name, Path(env) / name]
    result.append(_PACKAGE_DIR / triple / name)
    if (cargo := _repo_target_dir()) is not None:
        dev = [cargo / triple / "release" / name, cargo / triple / "debug" / name]
        if triple == host_triple():
            dev += [cargo / "release" / name, cargo / "debug" / name]
        # Prefer the most recently built development launcher.
        result += sorted((p for p in dev if p.is_file()), key=lambda p: -p.stat().st_mtime)
    return result


def find(target: Target, kind: Kind) -> Path:
    for path in candidates(target, kind):
        if path.is_file():
            return path
    raise BuildError(
        f"no {kind} launcher for {target_triple(target)} is available in this pybox "
        f"installation (development: `cargo build --release --target {target_triple(target)}`, "
        "or set PYBOX_LAUNCHER_DIR)"
    )
