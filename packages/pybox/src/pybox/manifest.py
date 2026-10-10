"""``pybox.json`` and entry-point code generation."""

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
    attr: str
    section: Literal["console", "gui"]

    @property
    def gui(self) -> bool:
        return self.section == "gui"


def entry_code(module: str, attr: str, *, pop_argv0: bool) -> str:
    """Python source that imports and calls ``module:attr``.

    With ``pop_argv0`` the code is meant for ``python -c CODE <exe> args...``:
    dropping the ``-c`` placeholder makes ``sys.argv[0]`` the launcher path, as
    with a regular console script.
    """
    for dotted in (module, attr):
        if not all(part.isidentifier() for part in dotted.split(".")):
            raise ValueError(f"invalid entry point {module}:{attr}")
    head = attr.split(".", 1)[0]
    lines = ["import sys"]
    if pop_argv0:
        lines.append("sys.argv.pop(0)")
    lines += [f"from {module} import {head}", f"sys.exit({attr}())"]
    return "; ".join(lines)


def default_entry_point(app_name: str, entry_points: Sequence[EntryPoint]) -> str | None:
    names = [ep.name for ep in entry_points]
    if app_name in names:
        return app_name
    console = [ep.name for ep in entry_points if not ep.gui]
    candidates = console or names
    return candidates[0] if candidates else None


def build(
    *,
    app_name: str,
    app_version: str,
    target: Target,
    interpreter: str,
    gui_interpreter: str | None,
    python_flags: Sequence[str],
    entry_points: Sequence[EntryPoint],
    pybox_version: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "app": {"name": app_name, "version": app_version},
        "interpreter": {
            "mode": "bundled",
            "path": interpreter,
            "gui_path": gui_interpreter,
            "version": ".".join(map(str, target.python)),
            "ext_suffix": target.ext_suffix,
        },
        "entry_points": {
            ep.name: {
                "args": [*python_flags, "-c", entry_code(ep.module, ep.attr, pop_argv0=True)],
                "gui": ep.gui,
                "value": f"{ep.module}:{ep.attr}",
            }
            for ep in entry_points
        },
        "default_entry_point": default_entry_point(app_name, entry_points),
        "target": {"name": str(target), "platform": target.platform_name},
        "build": {"tool": f"pybox {pybox_version}"},
    }


def dumps(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"
