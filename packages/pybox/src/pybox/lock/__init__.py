"""Resolution inputs: PEP 751 ``pylock.toml`` (preferred) or a pip installation report."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from packaging.utils import NormalizedName

ArtifactKind = Literal["wheel", "sdist", "directory", "vcs"]


@dataclasses.dataclass(frozen=True)
class Artifact:
    """One distribution selected for installation into a target."""

    name: NormalizedName
    version: str | None
    kind: ArtifactKind
    url: str | None = None
    path: Path | None = None
    """Absolute local path (wheel / sdist file, or source directory)."""
    hashes: Mapping[str, str] = dataclasses.field(default_factory=dict)
    filename: str | None = None
    subdirectory: str | None = None

    def describe(self) -> str:
        v = f" {self.version}" if self.version else ""
        src = self.filename or self.url or (str(self.path) if self.path else "?")
        return f"{self.name}{v} ({self.kind}: {src})"
