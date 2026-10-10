"""Remove files matching glob patterns from a staged tree.

Patterns are matched against POSIX paths relative to the tree root:
``*`` and ``?`` do not cross ``/``; ``**`` matches any number of directories.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from . import log


@lru_cache(maxsize=256)
def compile_glob(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip("/")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out))


def matches(rel_path: str, patterns: Sequence[str]) -> bool:
    return any(compile_glob(p).fullmatch(rel_path) for p in patterns)


def prune(root: Path, patterns: Sequence[str]) -> int:
    """Delete matching files (and directories left empty). Returns the number of files removed."""
    if not patterns:
        return 0
    removed = 0
    metadata_hit = False
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for fn in filenames:
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            if matches(rel, patterns):
                if ".dist-info/" in rel:
                    metadata_hit = True
                os.unlink(os.path.join(dirpath, fn))
                removed += 1
        if rel_dir != "." and not os.listdir(dirpath):
            os.rmdir(dirpath)
    if metadata_hit:
        log.warn("prune removed files inside *.dist-info; importlib.metadata may break")
    log.debug(f"pruned {removed} files")
    return removed
