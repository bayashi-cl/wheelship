"""AWS Lambda formats for the managed Python runtimes.

* ``lambda-zip``: deployment package; everything at the zip root (``/var/task``).
* ``lambda-layer``: dependencies under ``python/`` (``/opt/python``); console
  scripts under ``bin/`` (``/opt/bin`` is on ``PATH``).
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import log
from ..bundle import BuildContext
from ..errors import ConfigError
from ..install import flat_scheme, install_wheels
from ..reproducible import write_zip

# Limits from the Lambda quotas documentation.
MAX_DIRECT_UPLOAD = 50 * 1024 * 1024
MAX_UNZIPPED = 250 * 1024 * 1024

_HANDLER_RE = re.compile(r"^[A-Za-z_][\w.]*[:.][A-Za-z_]\w*$")


def lambda_handler(handler: str) -> tuple[str, str]:
    """Split ``pkg.mod:func`` (or Lambda's ``pkg.mod.func``) into module and function."""
    if not _HANDLER_RE.match(handler):
        raise ConfigError(f"invalid Lambda handler {handler!r}; expected 'package.module:function'")
    if ":" in handler:
        module, func = handler.split(":")
    else:
        module, _, func = handler.rpartition(".")
    if not module:
        raise ConfigError(f"invalid Lambda handler {handler!r}: module is missing")
    return module, func


def _interpreter(ctx: BuildContext) -> str:
    # Location of the interpreter in the managed runtimes (python3.x images).
    return f"/var/lang/bin/python{ctx.target.python_minor}"


def _module_exists(site: Path, module: str, ext_suffix: str) -> bool:
    base = site.joinpath(*module.split("."))
    return (
        base.with_suffix(".py").is_file()
        or (base / "__init__.py").is_file()
        or base.with_name(base.name + ext_suffix).is_file()
        or base.with_suffix(".abi3.so").is_file()
    )


def _check_size(out: Path, unzipped: int) -> None:
    zipped = out.stat().st_size
    log.info(f"  {out.name}: {zipped / 1e6:.1f} MB zipped, {unzipped / 1e6:.1f} MB unzipped")
    if zipped > MAX_DIRECT_UPLOAD:
        log.warn(f"{out.name} exceeds 50 MB; upload it through S3 instead of directly")
    if unzipped > MAX_UNZIPPED:
        log.warn(f"{out.name} exceeds Lambda's 250 MB unzipped limit (function + layers)")


def build_zip(ctx: BuildContext) -> Path:
    stage = ctx.stage("lambda-zip")
    install_wheels(ctx.wheels, flat_scheme(stage, stage), ctx.target, interpreter=_interpreter(ctx))
    ctx.finalize(stage)

    handler = ctx.cfg.lambda_.handler
    if handler is None:
        log.warn("[tool.pybox.lambda].handler is not set; the handler cannot be validated")
    else:
        module, func = lambda_handler(handler)
        if not _module_exists(stage, module, ctx.target.ext_suffix):
            raise ConfigError(f"Lambda handler module {module!r} is not in the package")
        log.info(f"  handler: {module}.{func}")

    out = ctx.artifact_path("lambda.zip")
    _check_size(out, write_zip(stage, out, executable=lambda name: name.startswith("bin/")))
    return out


def build_layer(ctx: BuildContext) -> Path:
    stage = ctx.stage("lambda-layer")
    wheels = ctx.wheels if ctx.cfg.lambda_.layer_include_app else ctx.dependencies
    if not wheels:
        log.warn("the layer is empty (the application has no dependencies)")
    site = stage / "python"
    site.mkdir()
    install_wheels(wheels, flat_scheme(stage, site), ctx.target, interpreter=_interpreter(ctx))
    ctx.finalize(stage)

    out = ctx.artifact_path("layer.zip")
    _check_size(out, write_zip(stage, out, executable=lambda name: name.startswith("bin/")))
    return out
