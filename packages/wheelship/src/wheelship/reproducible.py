"""Deterministic archive writers.

Entries are sorted, timestamps come from ``SOURCE_DATE_EPOCH`` (default:
1980-01-01, the zip epoch) and Unix permissions are set explicitly so that
archives built on Windows still carry executable bits for Linux targets.
Zip archives store symbolic links as regular files (dereferenced); tar keeps them.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import stat
import tarfile
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


def walk_files(root: Path, *, followlinks: bool = True) -> Iterator[tuple[str, Path]]:
    """Yield ``(posix relative path, absolute path)`` for files, sorted.

    Without ``followlinks``, symlinks to directories are yielded like files.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=followlinks):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if not followlinks:
            links = [d for d in dirnames if Path(dirpath, d).is_symlink()]
            dirnames[:] = [d for d in dirnames if d not in links]
            filenames = filenames + links
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
    exclude: ExecPredicate | None = None,
    symlinks: bool = False,
    compresslevel: int = 9,
) -> int:
    """Zip the contents of ``root`` into ``out``. Returns the uncompressed size.

    With ``symlinks`` symbolic links are stored as links (Unix mode ``S_IFLNK``,
    content = link target) instead of being dereferenced. ``exclude`` gets the
    path relative to ``root``.
    """
    date_time = time.gmtime(source_date_epoch())[:6]
    total = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        for rel, path in walk_files(root, followlinks=not symlinks):
            if exclude is not None and exclude(rel):
                continue
            name = f"{prefix.strip('/')}/{rel}" if prefix else rel
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.create_system = 3  # Unix, so external_attr permissions are honored
            if symlinks and path.is_symlink():
                info.external_attr = 0o120777 << 16
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, os.readlink(path).replace("\\", "/"))
                continue
            is_exec = _is_host_executable(path) or (executable is not None and executable(rel))
            info.external_attr = ((0o100755 if is_exec else 0o100644) & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = path.read_bytes()
            total += len(data)
            zf.writestr(info, data, compresslevel=compresslevel)
    os.replace(tmp, out)
    return total


def write_tar_gz(
    root: Path,
    out: Path,
    *,
    prefix: str = "",
    executable: ExecPredicate | None = None,
) -> int:
    """Tar+gzip ``root`` into ``out``, keeping symbolic links. Returns the uncompressed size."""
    epoch = source_date_epoch()
    prefix = prefix.strip("/")
    total = 0

    def info(name: str, kind: bytes, mode: int) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(f"{prefix}/{name}" if prefix else name)
        ti.type, ti.mode, ti.mtime = kind, mode, epoch
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.part")
    with (
        tmp.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf,
    ):
        if prefix:
            tf.addfile(info("", tarfile.DIRTYPE, 0o755))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            # Symlinks to directories show up in dirnames; store them as links.
            entries = sorted(filenames + [d for d in dirnames if Path(dirpath, d).is_symlink()])
            dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
            if rel_dir != ".":
                tf.addfile(info(rel_dir, tarfile.DIRTYPE, 0o755))
            for fn in entries:
                rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                path = Path(dirpath, fn)
                if path.is_symlink():
                    ti = info(rel, tarfile.SYMTYPE, 0o777)
                    ti.linkname = os.readlink(path).replace("\\", "/")
                    tf.addfile(ti)
                    continue
                is_exec = _is_host_executable(path) or (executable is not None and executable(rel))
                ti = info(rel, tarfile.REGTYPE, 0o755 if is_exec else 0o644)
                ti.size = path.stat().st_size
                total += ti.size
                with path.open("rb") as f:
                    tf.addfile(ti, f)
    os.replace(tmp, out)
    return total


def tree_digest(root: Path) -> str:
    """sha256 over the relative paths and contents of every file under ``root``."""
    h = hashlib.sha256()
    for rel, path in walk_files(root, followlinks=False):
        h.update(rel.encode() + b"\0")
        h.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()
