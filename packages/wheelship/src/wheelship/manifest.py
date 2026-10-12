"""``wheelship.json`` and entry-point code generation."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any, Literal

from .target import Target

SCHEMA = 1


@dataclasses.dataclass(frozen=True)
class EntryPoint:
    name: str
    module: str
    attr: str | None
    """None: run ``module`` as ``__main__``, like ``python -m``."""
    gui: bool = False
    args: tuple[str, ...] = ()
    """Inserted before the user's arguments."""

    @property
    def value(self) -> str:
        return self.module if self.attr is None else f"{self.module}:{self.attr}"


def entry_code(module: str, attr: str | None, *, pop_argv0: bool, args: Sequence[str] = ()) -> str:
    """Python source that imports and calls ``module:attr``.

    With ``pop_argv0`` the code is meant for ``python -c CODE <exe> args...``:
    dropping the ``-c`` placeholder makes ``sys.argv[0]`` the launcher path, as
    with a regular console script. ``args`` are inserted before the user's
    arguments. Without ``attr`` the module runs as ``__main__`` (``python -m``),
    which sets ``sys.argv[0]`` to the module's file.
    """
    for dotted in (module, attr or "_"):
        if not all(part.isidentifier() for part in dotted.split(".")):
            raise ValueError(f"invalid entry point {module}:{attr}")
    lines = ["import sys"]
    if pop_argv0:
        lines.append("sys.argv.pop(0)")
    if args:
        lines.append(f"sys.argv[1:1] = {list(args)!r}")
    if attr is None:
        lines += [
            "import runpy",
            f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)",
        ]
    else:
        head = attr.split(".", 1)[0]
        lines += [f"from {module} import {head}", f"sys.exit({attr}())"]
    return "; ".join(lines)


BOOT_SCRIPT = "_wheelship_boot.py"


def system_candidates(target: Target) -> list[dict[str, Any]]:
    """Interpreters the launcher looks for on ``PATH``, best first.

    The boot script verifies the version and ABI, so a wrong ``python3`` is
    reported clearly rather than failing on import.
    """
    minor = target.python_minor
    if target.os == "windows":
        return [
            {"program": "py", "gui_program": "pyw", "args": [f"-{minor}"]},
            {"program": "python", "gui_program": "pythonw"},
        ]
    return [{"program": f"python{minor}"}, {"program": "python3"}]


def build(
    *,
    app_name: str,
    app_version: str,
    target: Target,
    mode: Literal["bundled", "system"],
    python_flags: Sequence[str],
    entry_points: Sequence[EntryPoint],
    default_entry_point: str | None,
    wheelship_version: str,
    interpreter: str | None = None,
    gui_interpreter: str | None = None,
    library: str | None = None,
) -> dict[str, Any]:
    """The manifest document.

    bundled: ``interpreter``/``gui_interpreter`` are paths relative to the root;
    entry points run generated ``-c`` code, with ``app_args`` built in. system:
    the launcher finds an interpreter on ``PATH`` and runs the boot script,
    which checks the ABI, adds ``lib/`` to ``sys.path`` and inserts
    ``app_args`` before the user's arguments. ``library`` (bundled, Windows) is the
    ``python3XX.dll`` the launcher loads instead of spawning the interpreter
    when ``WHEELSHIP_EMBED=1`` (experimental).
    """
    interp: dict[str, Any] = {
        "mode": mode,
        "python": target.python_minor,
        "version": ".".join(map(str, target.python)) if target.python_exact else None,
        "ext_suffix": target.ext_suffix,
    }
    if mode == "bundled":
        if interpreter is None:
            raise ValueError("bundled manifests need the interpreter path")
        interp |= {"path": interpreter, "gui_path": gui_interpreter}
        if library is not None:
            interp["library"] = library
    else:
        interp["candidates"] = system_candidates(target)

    def args(ep: EntryPoint) -> list[str]:
        if mode == "bundled":
            return [
                *python_flags,
                "-c",
                entry_code(ep.module, ep.attr, pop_argv0=True, args=ep.args),
            ]
        # -S: the interpreter's own site-packages (system or venv) stay out of
        # sys.path; the boot script adds only the bundle's lib/.
        no_site = [] if "-S" in python_flags else ["-S"]
        return [*python_flags, *no_site, f"{{root}}/{BOOT_SCRIPT}", ep.name]

    return {
        "schema": SCHEMA,
        "app": {"name": app_name, "version": app_version},
        "interpreter": interp,
        "entry_points": {
            ep.name: {
                "args": args(ep),
                "app_args": list(ep.args),
                "gui": ep.gui,
                "value": ep.value,
            }
            for ep in entry_points
        },
        "default_entry_point": default_entry_point,
        "target": {"name": str(target), "platform": target.platform_name},
        "build": {"tool": f"wheelship {wheelship_version}"},
    }


def dumps(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"
