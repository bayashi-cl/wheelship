"""Select artifacts for a :class:`~pybox.target.Target` from a PEP 751 lock file.

Selection itself is delegated to :meth:`packaging.pylock.Pylock.select`, which
implements the installer algorithm from the specification; we only feed it the
synthesized marker environment and tags of the target.
"""

from __future__ import annotations

import posixpath
import tomllib
import urllib.parse
from collections.abc import Collection
from pathlib import Path

from packaging.pylock import (
    PackageArchive,
    PackageDirectory,
    PackageSdist,
    PackageVcs,
    PackageWheel,
    Pylock,
    PylockSelectError,
    PylockUnsupportedVersionError,
    PylockValidationError,
)

from ..errors import ResolveError
from ..target import Target
from . import Artifact, ArtifactKind


def load(path: Path) -> Pylock:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ResolveError(f"lock file {path} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ResolveError(f"{path}: {e}") from None
    try:
        return Pylock.from_dict(data)
    except (PylockValidationError, PylockUnsupportedVersionError) as e:
        raise ResolveError(f"{path}: invalid lock file: {e}") from None


def select(
    lock_path: Path,
    target: Target,
    *,
    extras: Collection[str] = (),
    groups: Collection[str] | None = None,
) -> list[Artifact]:
    lock = load(lock_path)
    base = lock_path.parent.resolve()
    try:
        selected = list(
            lock.select(
                environment=target.marker_environment(),
                tags=target.tags(),
                extras=extras,
                dependency_groups=groups,
            )
        )
    except PylockSelectError as e:
        raise ResolveError(f"{lock_path}: cannot select packages for {target}: {e}") from None

    result: list[Artifact] = []
    for package, source in selected:
        version = str(package.version) if package.version else None
        name = package.name
        if isinstance(source, PackageWheel):
            result.append(
                Artifact(
                    name, version, "wheel",
                    url=source.url,
                    path=_local(base, source.path),
                    hashes=source.hashes,
                    filename=source.filename,
                )
            )  # fmt: skip
        elif isinstance(source, PackageSdist):
            result.append(
                Artifact(
                    name, version, "sdist",
                    url=source.url,
                    path=_local(base, source.path),
                    hashes=source.hashes,
                    filename=source.filename,
                )
            )  # fmt: skip
        elif isinstance(source, PackageDirectory):
            # ``editable`` is ignored on purpose: bundles always get a real install.
            result.append(
                Artifact(
                    name, version, "directory",
                    path=_local(base, source.path),
                    subdirectory=source.subdirectory,
                )
            )  # fmt: skip
        elif isinstance(source, PackageArchive):
            filename = _archive_filename(source)
            kind: ArtifactKind = "wheel" if filename.endswith(".whl") else "sdist"
            result.append(
                Artifact(
                    name, version, kind,
                    url=source.url,
                    path=_local(base, source.path),
                    hashes=source.hashes,
                    filename=filename,
                    subdirectory=source.subdirectory,
                )
            )  # fmt: skip
        elif isinstance(source, PackageVcs):
            result.append(
                Artifact(
                    name, version, "vcs",
                    url=source.url,
                    path=_local(base, source.path),
                    subdirectory=source.subdirectory,
                )
            )  # fmt: skip
        else:  # pragma: no cover - exhaustive
            raise ResolveError(f"unsupported source for {name}: {source!r}")
    return result


def _local(base: Path, path: str | None) -> Path | None:
    if path is None:
        return None
    return (base / path).resolve()


def _archive_filename(source: PackageArchive) -> str:
    ref = source.url or source.path or ""
    return posixpath.basename(urllib.parse.urlsplit(ref).path.replace("\\", "/"))
