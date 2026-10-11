"""Formats shared by both interpreter modes: ``dir``, ``archive`` and ``exe``.

A target's *tree* is assembled once (``bundled.bundled_tree`` or
``system.system_tree``) and then packaged::

    <bundle>/
    ├── <script>(.exe)       launcher per [tool.wheelship.scripts] entry
    ├── wheelship.json       manifest read by the launcher
    ├── python/              bundled: python-build-standalone, app installed into it
    └── lib/ + _wheelship_boot.py   system: site directory + bootstrap

``exe`` appends the tree as a zip payload to a single launcher; it is
extracted once into a per-user cache on first run (see the launcher's
``payload`` module for the file layout).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
from collections.abc import Sequence
from pathlib import Path

from .. import bytecode, log, manifest, prune
from ..bundle import BuildContext
from ..errors import BuildError, ConfigError
from ..manifest import EntryPoint
from ..reproducible import write_tar_gz, write_zip
from ..wheels import entry_points

PAYLOAD_MAGIC = b"WHSHPAY1"


def tree(ctx: BuildContext) -> Path:
    if ctx.tree_root is None:
        if ctx.tc.interpreter == "bundled":
            from .bundled import bundled_tree

            ctx.tree_root = bundled_tree(ctx)
        else:
            from .system import system_tree

            ctx.tree_root = system_tree(ctx)
    return ctx.tree_root


# -- helpers used by the tree builders -----------------------------------------


def bundle_entry_points(ctx: BuildContext) -> list[EntryPoint]:
    """``[tool.wheelship.scripts]`` with ``script`` references resolved."""
    if ctx.entry_points is None:
        ctx.entry_points = _resolve_scripts(ctx)
    return ctx.entry_points


def _resolve_scripts(ctx: BuildContext) -> list[EntryPoint]:
    cfg = ctx.cfg
    if not cfg.scripts:
        own = [ep.name for ep in entry_points(ctx.app.path)]
        example = (
            f'{own[0]} = {{ script = "{own[0]}" }}' if own else 'app = { entry = "pkg.cli:main" }'
        )
        raise ConfigError(
            "[tool.wheelship.scripts] is empty; a bundle needs at least one entry point "
            f"to launch, for example:\n  [tool.wheelship.scripts]\n  {example}"
        )
    declared: dict[str, list[tuple[str, EntryPoint]]] = {}
    if any(s.script is not None for s in cfg.scripts):
        for w in ctx.wheels:
            for ep in entry_points(w.path):
                declared.setdefault(ep.name, []).append((w.artifact.name, ep))

    eps = []
    for s in cfg.scripts:
        where = f"[tool.wheelship.scripts] {s.name!r}"
        if s.script is not None:
            found = declared.get(s.script, [])
            if not found:
                known = ", ".join(sorted(declared)) or "(none)"
                raise ConfigError(
                    f"{where}: no distribution in the bundle declares a script named "
                    f"{s.script!r} (available: {known})"
                )
            if len(found) > 1:
                dists = ", ".join(dist for dist, _ in found)
                raise ConfigError(f"{where}: script {s.script!r} is declared by {dists}")
            ref = found[0][1]
            module, attr, gui = ref.module, ref.attr, ref.gui
        else:
            assert s.entry is not None
            module, sep, attr_ = s.entry.partition(":")
            attr, gui = (attr_ if sep else None), False
        eps.append(
            EntryPoint(s.name, module, attr, gui=gui if s.gui is None else s.gui, args=s.args)
        )
    return eps


def launcher_name(ctx: BuildContext, ep: EntryPoint) -> str:
    return f"{ep.name}.exe" if ctx.target.os == "windows" else ep.name


def install_launchers(ctx: BuildContext, root: Path, eps: Sequence[EntryPoint]) -> None:
    for ep in eps:
        dest = root / launcher_name(ctx, ep)
        if dest.exists():
            raise BuildError(f"entry point {ep.name!r} clashes with {dest.name} in the bundle")
        shutil.copyfile(ctx.launcher("gui" if ep.gui else "console"), dest)
        dest.chmod(0o755)


def write_manifest(root: Path, doc: dict[str, object]) -> None:
    (root / "wheelship.json").write_text(manifest.dumps(doc), encoding="utf-8")


def finalize(ctx: BuildContext, root: Path, compile_dir: Path, *, python: str | None) -> None:
    prune.prune(root, ctx.cfg.prune)
    if ctx.compile_bytecode:
        # force: python-build-standalone ships timestamp-based pycs.
        bytecode.compile_tree(compile_dir, ctx.target, python=python, force=True)


def executable(ctx: BuildContext, rel: str) -> bool:
    """Files that must be executable on Linux, independent of host permissions."""
    if ctx.target.os != "linux":
        return False
    return (
        rel.startswith("python/bin/")
        or ".so" in rel.rsplit("/", 1)[-1]
        or ("/" not in rel and not rel.endswith((".json", ".py")))  # launchers
    )


# -- formats -------------------------------------------------------------------


def build_dir(ctx: BuildContext) -> Path:
    src = tree(ctx)
    dest = ctx.out_dir / ctx.artifact_stem
    if dest.exists():
        if not (dest / "wheelship.json").is_file():
            raise BuildError(f"{dest} exists and is not a wheelship bundle; refusing to replace it")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=True)
    size = sum(p.lstat().st_size for p in dest.rglob("*") if p.is_file() and not p.is_symlink())
    log.info(f"  {dest.name}/ ({size / 1e6:.1f} MB)")
    return dest


def build_archive(ctx: BuildContext) -> Path:
    src = tree(ctx)
    stem = ctx.artifact_stem
    if ctx.target.os == "windows":
        out = ctx.out_dir / f"{stem}.zip"
        size = write_zip(src, out, prefix=stem)
    else:
        out = ctx.out_dir / f"{stem}.tar.gz"
        size = write_tar_gz(src, out, prefix=stem, executable=lambda r: executable(ctx, r))
    log.info(f"  {out.name}: {out.stat().st_size / 1e6:.1f} MB ({size / 1e6:.1f} MB unpacked)")
    return out


def build_exe(ctx: BuildContext) -> Path:
    src = tree(ctx)
    eps = bundle_entry_points(ctx)
    doc = json.loads((src / "wheelship.json").read_text(encoding="utf-8"))
    default = next(ep for ep in eps if ep.name == doc["default_entry_point"])
    stub = ctx.launcher("gui" if default.gui else "console").read_bytes()

    # The outer executable is the launcher: the per-script copies are not needed.
    root_launchers = {launcher_name(ctx, ep) for ep in eps}
    payload = ctx.work / f"{ctx.tc.name}-payload.zip"
    size = write_zip(
        src,
        payload,
        symlinks=True,
        executable=lambda r: executable(ctx, r),
        exclude=lambda r: r in root_launchers,
    )
    data = payload.read_bytes()
    meta = json.dumps(
        {"name": ctx.cfg.name, "sha256": hashlib.sha256(data).hexdigest()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    # Linux: ".bin" keeps it apart from the dir format's output directory.
    suffix = ".exe" if ctx.target.os == "windows" else ".bin"
    out = ctx.out_dir / f"{ctx.artifact_stem}{suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    with tmp.open("wb") as f:
        f.write(stub)
        f.write(data)
        f.write(meta)
        f.write(PAYLOAD_MAGIC + struct.pack("<QQQ", len(stub), len(data), len(meta)))
    tmp.chmod(0o755)
    tmp.replace(out)
    log.info(f"  {out.name}: {out.stat().st_size / 1e6:.1f} MB ({size / 1e6:.1f} MB unpacked)")
    if len(eps) > 1:
        others = ", ".join(ep.name for ep in eps if ep is not default)
        log.info(f"  runs {default.name!r}; copy or rename it to {others} for the others")
    return out
