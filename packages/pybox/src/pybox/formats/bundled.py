"""Bundles with a python-build-standalone interpreter: ``dir`` and ``archive``.

Layout::

    <bundle>/
    ├── <script>(.exe)    launcher per console/gui script of the application
    ├── pybox.json        manifest read by the launcher
    └── python/           PBS install_only tree; the app and its dependencies are
                          installed into its own site-packages (as pip would)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import __version__, bytecode, launchers, log, manifest, pbs, prune
from ..bundle import BuildContext
from ..errors import BuildError, ConfigError
from ..install import Scheme, install_wheels
from ..reproducible import write_tar_gz, write_zip
from ..wheels import entry_points


def bundled_tree(ctx: BuildContext) -> Path:
    """Assemble the bundle once per target; every bundled format reuses it."""
    if ctx.bundled_root is not None:
        return ctx.bundled_root
    asset, target, cfg = ctx.pbs_asset, ctx.target, ctx.cfg
    assert asset is not None, "bundled builds resolve python-build-standalone first"

    app_entry_points = entry_points(ctx.app.path)
    if not app_entry_points:
        raise ConfigError(
            f"{cfg.name} defines no [project.scripts] or [project.gui-scripts]; "
            "a bundle needs at least one entry point to launch"
        )

    root = ctx.stage("bundled")
    log.info(f"  python-build-standalone: {asset.name}")
    python_dir = pbs.extract(pbs.download(asset), root)
    pbs.trim(python_dir, target, cfg.pbs.strip)

    layout = pbs.Layout.for_target(target)
    scheme = Scheme(
        purelib=python_dir / layout.site_packages,
        platlib=python_dir / layout.site_packages,
        headers=python_dir / layout.include / "site" / f"python{target.python_minor}",
        scripts=python_dir / layout.scripts,
        data=python_dir,
    )
    interpreter_from_scripts = Path(
        os.path.relpath(python_dir / layout.interpreter, scheme.scripts)
    ).as_posix()
    install_wheels(
        ctx.wheels,
        scheme,
        target,
        interpreter=interpreter_from_scripts,
        relocatable=True,
        python_flags=cfg.python_flags,
    )

    suffix = ".exe" if target.os == "windows" else ""
    for ep in app_entry_points:
        dest = root / f"{ep.name}{suffix}"
        if dest.exists():
            raise BuildError(f"entry point {ep.name!r} clashes with {dest.name} in the bundle")
        shutil.copyfile(launchers.find(target, "gui" if ep.gui else "console"), dest)
        dest.chmod(0o755)

    data = manifest.build(
        app_name=cfg.name,
        app_version=ctx.version,
        target=target,
        interpreter=f"python/{layout.interpreter}",
        gui_interpreter=f"python/{layout.gui_interpreter}" if layout.gui_interpreter else None,
        python_flags=cfg.python_flags,
        entry_points=app_entry_points,
        pybox_version=__version__,
    )
    (root / "pybox.json").write_text(manifest.dumps(data), encoding="utf-8")

    prune.prune(root, cfg.prune)
    if ctx.compile_bytecode:
        # The bundled interpreter itself can compile when it runs on the host.
        own = python_dir / layout.interpreter
        python = str(own) if pbs.target_triple(target) == pbs.host_triple() else None
        bytecode.compile_tree(python_dir, target, python=python, force=True)

    ctx.bundled_root = root
    return root


def _executable(ctx: BuildContext, rel: str) -> bool:
    """Files that must be executable on Linux, independent of host permissions."""
    if ctx.target.os != "linux":
        return False
    return (
        rel.startswith("python/bin/")
        or ".so" in rel.rsplit("/", 1)[-1]
        or ("/" not in rel and rel != "pybox.json")  # launchers
    )


def build_dir(ctx: BuildContext) -> Path:
    tree = bundled_tree(ctx)
    dest = ctx.out_dir / ctx.artifact_stem
    if dest.exists():
        if not (dest / "pybox.json").is_file():
            raise BuildError(f"{dest} exists and is not a pybox bundle; refusing to replace it")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tree, dest, symlinks=True)
    log.info(f"  {dest.name}/ ({_tree_size(dest) / 1e6:.1f} MB)")
    return dest


def build_archive(ctx: BuildContext) -> Path:
    tree = bundled_tree(ctx)
    stem = ctx.artifact_stem
    if ctx.target.os == "windows":
        out = ctx.out_dir / f"{stem}.zip"
        size = write_zip(tree, out, prefix=stem)
    else:
        out = ctx.out_dir / f"{stem}.tar.gz"
        size = write_tar_gz(tree, out, prefix=stem, executable=lambda r: _executable(ctx, r))
    log.info(f"  {out.name}: {out.stat().st_size / 1e6:.1f} MB ({size / 1e6:.1f} MB unpacked)")
    return out


def _tree_size(root: Path) -> int:
    return sum(p.lstat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
