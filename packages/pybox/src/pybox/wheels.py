"""Turn selected artifacts into wheel files compatible with the target.

* wheels are downloaded (or taken from a local path) and hash-verified;
* local directories (typically the application itself) and sdists are built
  with a PEP 517 frontend (``build``) on the host — the produced wheel must
  then still be compatible with the target, otherwise we ask for a prebuilt
  wheel via ``find-links``.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from installer.sources import WheelFile
from installer.utils import parse_entrypoints
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, canonicalize_version, parse_wheel_filename

from . import fetch, log
from .errors import BuildError, FetchError
from .lock import Artifact
from .manifest import EntryPoint
from .target import Target


@dataclasses.dataclass(frozen=True)
class Wheel:
    artifact: Artifact
    path: Path

    @property
    def version(self) -> str:
        return str(parse_wheel_filename(self.path.name)[1])


def entry_points(wheel: Path) -> list[EntryPoint]:
    """console_scripts / gui_scripts declared by a wheel."""
    with WheelFile.open(wheel) as source:
        try:
            text = source.read_dist_info("entry_points.txt")
        except (KeyError, FileNotFoundError):
            return []
    return [EntryPoint(*ep) for ep in parse_entrypoints(text)]


def is_compatible(filename: str, target: Target) -> bool:
    try:
        wheel_tags = parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return False
    return not wheel_tags.isdisjoint(target.tags())


def obtain(
    artifacts: Sequence[Artifact],
    target: Target,
    work: Path,
    *,
    find_links: Sequence[Path] = (),
    jobs: int = 8,
) -> list[Wheel]:
    """Return one target-compatible wheel per artifact (order preserved)."""
    tags = target.tags()
    local_wheels = _scan_find_links(find_links)

    def one(a: Artifact) -> Wheel:
        if a.kind == "wheel":
            return Wheel(a, _get_file(a))
        if a.kind == "vcs":
            raise BuildError(f"{a.describe()}: VCS sources are not supported yet")
        if a.kind == "sdist" and (prebuilt := _find_prebuilt(a, local_wheels, tags)):
            log.debug(f"using prebuilt wheel {prebuilt.name} for {a.name}")
            return Wheel(a, prebuilt)
        return Wheel(a, _build(a, target, work))

    # Downloads and builds run concurrently; each build has its own output directory.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        wheels = list(pool.map(one, artifacts))

    for w in wheels:
        if not is_compatible(w.path.name, target):
            hint = (
                " (built on the host for a different platform; provide a prebuilt "
                "wheel through [tool.pybox].find-links)"
                if w.artifact.kind != "wheel"
                else ""
            )
            raise BuildError(f"{w.path.name} is not compatible with target {target}{hint}")
    return wheels


def _get_file(a: Artifact) -> Path:
    assert a.filename is not None
    if a.path is not None:
        if not a.path.is_file():
            raise FetchError(f"{a.describe()}: {a.path} does not exist")
        fetch.verify(a.path, a.hashes, a.filename)
        return a.path
    if a.url is None:
        raise FetchError(f"{a.describe()}: neither url nor path is set")
    return fetch.fetch(a.url, a.filename, a.hashes)


def _scan_find_links(dirs: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            log.warn(f"find-links directory {d} does not exist")
            continue
        found.extend(sorted(d.glob("*.whl")))
    return found


def _find_prebuilt(a: Artifact, candidates: Sequence[Path], tags: Sequence[Tag]) -> Path | None:
    priority = {t: i for i, t in enumerate(tags)}
    best: tuple[int, Path] | None = None
    for path in candidates:
        try:
            name, version, _, wtags = parse_wheel_filename(path.name)
        except InvalidWheelFilename:
            continue
        if name != a.name:
            continue
        if a.version and canonicalize_version(version) != canonicalize_version(a.version):
            continue
        ranks = [priority[t] for t in wtags if t in priority]
        if ranks and (best is None or min(ranks) < best[0]):
            best = (min(ranks), path)
    return best[1] if best else None


def _build(a: Artifact, target: Target, work: Path) -> Path:
    if a.kind == "directory":
        assert a.path is not None
        src = a.path / a.subdirectory if a.subdirectory else a.path
        return build_wheel(src, work / "built" / a.name)
    # sdist
    archive = _get_file(a)
    extract = Path(tempfile.mkdtemp(prefix=f"{a.name}-", dir=work))
    root = _extract_sdist(archive, extract)
    if a.subdirectory:
        root = root / a.subdirectory
    log.info(f"  building {a.name} {a.version or ''} from sdist (host: {sys.platform})")
    return build_wheel(root, work / "built" / a.name)


def build_wheel(source_dir: Path, out_dir: Path) -> Path:
    """Build a wheel with the PEP 517 frontend ``build`` in an isolated environment."""
    if not (source_dir / "pyproject.toml").is_file() and not (source_dir / "setup.py").is_file():
        raise BuildError(f"{source_dir} is not a Python project (no pyproject.toml/setup.py)")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir), str(source_dir)]
    if shutil.which("uv"):
        cmd.insert(4, "--installer=uv")
    log.debug(" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        output = proc.stdout[-4000:] + proc.stderr[-4000:]
        raise BuildError(f"failed to build a wheel from {source_dir}:\n{output}")
    built = sorted(out_dir.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    if not built:
        raise BuildError(f"building {source_dir} produced no wheel")
    return built[-1]


def _extract_sdist(archive: Path, dest: Path) -> Path:
    name = archive.name
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        raise BuildError(f"unsupported sdist format: {name}")
    entries = [p for p in dest.iterdir()]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else dest
