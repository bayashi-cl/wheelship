"""Deterministic archive writers.

Entries are sorted, timestamps come from ``SOURCE_DATE_EPOCH`` (default:
1980-01-01, the zip epoch) and Unix permissions are set explicitly so that
archives built on Windows still carry executable bits for Linux targets.
Symbolic links are stored as regular files (dereferenced).
"""

from __future__ import annotations

import os
import stat
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

_ZIP_EPOCH = 315532800  # 1980-01-01T00:00:00Z

ExecPredicate = Callable[[str], bool]


def source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return _ZIP_EPOCH
    return max(int(raw), _ZIP_EPOCH)


def walk_files(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(posix relative path, absolute path)`` for files, sorted."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for fn in sorted(filenames):
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            yield rel, Path(dirpath, fn)


def _is_host_executable(path: Path) -> bool:
    return os.name != "nt" and bool(path.stat().st_mode & stat.S_IXUSR)


def write_zip(
    root: Path,
    out: Path,
    *,
    prefix: str = "",
    executable: ExecPredicate | None = None,
    compresslevel: int = 9,
) -> int:
    """Zip the contents of ``root`` into ``out``. Returns the uncompressed size."""
    date_time = time.gmtime(source_date_epoch())[:6]
    total = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        for rel, path in walk_files(root):
            name = f"{prefix.strip('/')}/{rel}" if prefix else rel
            is_exec = _is_host_executable(path) or (executable is not None and executable(name))
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.create_system = 3  # Unix, so external_attr permissions are honored
            info.external_attr = ((0o100755 if is_exec else 0o100644) & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = path.read_bytes()
            total += len(data)
            zf.writestr(info, data, compresslevel=compresslevel)
    os.replace(tmp, out)
    return total
