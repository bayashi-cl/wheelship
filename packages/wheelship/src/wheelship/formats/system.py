"""Bundles for an interpreter installed on the target: tree and ``pyz``.

Tree::

    <bundle>/
    ├── <script>(.exe)        launcher: finds the interpreter on PATH
    ├── wheelship.json
    ├── _wheelship_boot.py    checks the ABI, adds lib/ to sys.path, runs the entry point
    └── lib/                  purelib = platlib (a site directory)

``pyz`` is a PEP 441 zipapp of the same content with the bootstrap as
``__main__.py``; it extracts ``lib/`` into the cache on first run because
native extensions cannot be imported from a zip.

Console scripts of dependencies are not generated: without a bundled
interpreter there is nothing they could reliably point at.
"""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

from .. import __version__, log, manifest
from ..bundle import BuildContext
from ..errors import ConfigError
from ..install import Scheme, install_wheels
from ..reproducible import tree_digest, write_zip
from .package import app_entry_points, finalize, install_launchers, tree, write_manifest


def boot_script() -> bytes:
    return resources.files("wheelship").joinpath("_boot.py").read_bytes()


def system_tree(ctx: BuildContext) -> Path:
    target, cfg = ctx.target, ctx.cfg
    eps = app_entry_points(ctx)

    root = ctx.stage("system")
    lib = root / "lib"
    discard = ctx.work / "discarded-scripts"
    scheme = Scheme(
        purelib=lib,
        platlib=lib,
        headers=discard / "include",
        scripts=discard / "bin",
        data=root / "data",
    )
    install_wheels(ctx.wheels, scheme, target, interpreter="python")
    (root / manifest.BOOT_SCRIPT).write_bytes(boot_script())

    install_launchers(ctx, root, eps)
    write_manifest(
        root,
        manifest.build(
            app_name=cfg.name,
            app_version=ctx.version,
            target=target,
            mode="system",
            python_flags=cfg.python_flags,
            entry_points=eps,
            wheelship_version=__version__,
        ),
    )
    finalize(ctx, root, lib, python=None)
    return root


def build_pyz(ctx: BuildContext) -> Path:
    if ctx.tc.interpreter != "system":
        raise ConfigError("pyz runs on the target's interpreter: set interpreter = 'system'")
    src = tree(ctx)
    stage = ctx.stage("pyz")
    shutil.copytree(src / "lib", stage / "lib", symlinks=True)
    doc = json.loads((src / "wheelship.json").read_text(encoding="utf-8"))
    # Identifies the extracted copy in the cache (like the exe payload hash).
    doc["payload"] = {"sha256": tree_digest(stage / "lib")}
    write_manifest(stage, doc)
    (stage / "__main__.py").write_bytes(boot_script())

    archive = ctx.work / f"{ctx.tc.name}.pyz.zip"
    size = write_zip(stage, archive)
    out = ctx.out_dir / f"{ctx.artifact_stem}.pyz"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    # Shebang as in PEP 441; on Windows the py launcher honors it too.
    tmp.write_bytes(
        f"#!/usr/bin/env python{ctx.target.python_minor}\n".encode() + archive.read_bytes()
    )
    tmp.chmod(0o755)
    tmp.replace(out)
    log.info(f"  {out.name}: {out.stat().st_size / 1e6:.1f} MB ({size / 1e6:.1f} MB unpacked)")
    return out
