"""Install wheels into a target scheme with :mod:`installer` (PEP 427 / RECORD)."""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.records import RecordEntry
from installer.sources import WheelFile
from installer.utils import Scheme as InstallerScheme

from . import __version__, log
from .errors import BuildError
from .manifest import entry_code
from .target import Target
from .wheels import Wheel

if TYPE_CHECKING:
    from installer.scripts import ScriptSection

# Runs on any POSIX sh, and is also valid Python: line 2 is a string literal
# to Python but `exec`s the interpreter next to the script for the shell.
_POSIX_SCRIPT = """#!/bin/sh
'''exec' "$(dirname -- "$(realpath -- "$0")")/{interpreter}" {flags}"$0" "$@"
' '''
{code}
"""

_WINDOWS_SCRIPT = """@echo off\r
"%~dp0{interpreter}" {flags}-c "{code}" "%~f0" %*\r
exit /b %ERRORLEVEL%\r
"""


@dataclasses.dataclass(frozen=True)
class Scheme:
    """Absolute directories for each wheel install scheme key."""

    purelib: Path
    platlib: Path
    headers: Path
    scripts: Path
    data: Path

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in dataclasses.asdict(self).items()}


def flat_scheme(prefix: Path, site: Path) -> Scheme:
    """purelib = platlib = ``site``; everything else relative to ``prefix``."""
    return Scheme(
        purelib=site,
        platlib=site,
        headers=prefix / "include",
        scripts=prefix / "bin",
        data=prefix,
    )


class RelocatableDestination(SchemeDictionaryDestination):
    """Writes console scripts that find the interpreter relative to themselves.

    installer's own scripts embed an absolute interpreter path (and on Windows a
    launcher with an absolute shebang), which breaks once the bundle moves.
    """

    def __init__(
        self,
        *args: Any,
        relative_interpreter: str,
        python_flags: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.relative_interpreter = relative_interpreter
        self.python_flags = tuple(python_flags)

    def write_script(
        self, name: str, module: str, attr: str, section: ScriptSection
    ) -> RecordEntry:
        flags = "".join(f"{f} " for f in self.python_flags)
        if self.script_kind == "posix":
            filename = name
            data = _POSIX_SCRIPT.format(
                interpreter=self.relative_interpreter,
                flags=flags,
                code=entry_code(module, attr, pop_argv0=False),
            )
        else:
            filename = f"{name}.cmd"
            data = _WINDOWS_SCRIPT.format(
                interpreter=self.relative_interpreter.replace("/", "\\"),
                flags=flags,
                code=entry_code(module, attr, pop_argv0=True),
            )
        with io.BytesIO(data.encode()) as stream:
            return self.write_to_fs(
                InstallerScheme("scripts"), filename, stream, is_executable=True
            )


def install_wheels(
    wheels: Iterable[Wheel],
    scheme: Scheme,
    target: Target,
    *,
    interpreter: str,
    relocatable: bool = False,
    python_flags: Sequence[str] = (),
) -> None:
    """Install each wheel.

    ``interpreter`` is the shebang of generated scripts, or with ``relocatable``
    the interpreter path relative to the scripts directory.
    """
    common: dict[str, Any] = {
        "interpreter": interpreter,
        "script_kind": target.script_kind,
        # Bytecode is compiled later with an interpreter matching the target.
        "bytecode_optimization_levels": (),
    }
    destination: SchemeDictionaryDestination
    if relocatable:
        destination = RelocatableDestination(
            scheme.as_dict(),
            relative_interpreter=interpreter,
            python_flags=python_flags,
            **common,
        )
    else:
        destination = SchemeDictionaryDestination(scheme.as_dict(), **common)
    for wheel in wheels:
        log.debug(f"installing {wheel.path.name}")
        try:
            with WheelFile.open(wheel.path) as source:
                install(
                    source,
                    destination,
                    additional_metadata={"INSTALLER": f"wheelship {__version__}\n".encode()},
                )
        except FileExistsError as e:
            raise BuildError(f"{wheel.path.name}: file conflicts with another package: {e}") from e
        except Exception as e:  # installer raises plain exceptions for bad wheels
            raise BuildError(f"failed to install {wheel.path.name}: {e}") from e
