"""Tree for bundles with a python-build-standalone interpreter.

The application and its dependencies are installed into the interpreter's own
site-packages, exactly where pip would put them.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import __version__, log, manifest, pbs
from ..bundle import BuildContext
from ..install import Scheme, install_wheels
from .package import app_entry_points, finalize, install_launchers, write_manifest


def bundled_tree(ctx: BuildContext) -> Path:
    asset, target, cfg = ctx.pbs_asset, ctx.target, ctx.cfg
    assert asset is not None, "bundled builds resolve python-build-standalone first"
    eps = app_entry_points(ctx)

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

    install_launchers(ctx, root, eps)
    write_manifest(
        root,
        manifest.build(
            app_name=cfg.name,
            app_version=ctx.version,
            target=target,
            mode="bundled",
            interpreter=f"python/{layout.interpreter}",
            gui_interpreter=f"python/{layout.gui_interpreter}" if layout.gui_interpreter else None,
            python_flags=cfg.python_flags,
            entry_points=eps,
            wheelship_version=__version__,
        ),
    )
    # The bundled interpreter itself can compile when it runs on the host.
    own = python_dir / layout.interpreter
    runnable = pbs.target_triple(target) == pbs.host_triple()
    finalize(ctx, root, python_dir, python=str(own) if runnable else None)
    return root
