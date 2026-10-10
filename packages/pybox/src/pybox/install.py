"""Install wheels into a target scheme with :mod:`installer` (PEP 427 / RECORD)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from pathlib import Path

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile

from . import __version__, log
from .errors import BuildError
from .target import Target
from .wheels import Wheel


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


def install_wheels(
    wheels: Iterable[Wheel],
    scheme: Scheme,
    target: Target,
    *,
    interpreter: str,
) -> None:
    """Install each wheel; ``interpreter`` becomes the shebang of generated scripts."""
    destination = SchemeDictionaryDestination(
        scheme.as_dict(),
        interpreter=interpreter,
        script_kind=target.script_kind,
        # Bytecode is compiled later with an interpreter matching the target.
        bytecode_optimization_levels=(),
    )
    for wheel in wheels:
        log.debug(f"installing {wheel.path.name}")
        try:
            with WheelFile.open(wheel.path) as source:
                install(
                    source,
                    destination,
                    additional_metadata={"INSTALLER": f"pybox {__version__}\n".encode()},
                )
        except FileExistsError as e:
            raise BuildError(f"{wheel.path.name}: file conflicts with another package: {e}") from e
        except Exception as e:  # installer raises plain exceptions for bad wheels
            raise BuildError(f"failed to install {wheel.path.name}: {e}") from e
