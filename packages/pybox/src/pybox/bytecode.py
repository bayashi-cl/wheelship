"""Pre-compile bytecode with an interpreter of the target's Python version.

``.pyc`` files are platform independent but version specific, so for cross
builds we only need *some* CPython of the same minor version on the host.
Files are compiled in ``unchecked-hash`` mode (PEP 552): reproducible, and the
runtime never needs to stat sources or write caches (read-only ``/var/task``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import log
from .target import Target


def find_interpreter(target: Target) -> str | None:
    """Locate a host CPython matching ``target``'s minor version.

    Order: ``PYBOX_BYTECODE_PYTHON``, the running interpreter, ``uv python find``.
    (Phase 1 adds python-build-standalone as the final fallback.)
    """
    want = target.python[:2]
    if env := os.environ.get("PYBOX_BYTECODE_PYTHON"):
        return env
    if sys.implementation.name == "cpython" and sys.version_info[:2] == want:
        return sys.executable
    if uv := shutil.which("uv"):
        proc = subprocess.run(
            [uv, "python", "find", "--no-project", f"cpython@{want[0]}.{want[1]}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def compile_tree(root: Path, target: Target) -> bool:
    """Compile every ``.py`` under ``root``. Returns False if skipped."""
    python = find_interpreter(target)
    if python is None:
        log.warn(
            f"no CPython {target.python_minor} found on the host; skipping bytecode compilation "
            "(set PYBOX_BYTECODE_PYTHON or install one with `uv python install`)"
        )
        return False
    cmd = [
        python, "-m", "compileall", "-q", "-j", "0",
        "--invalidation-mode", "unchecked-hash",
        # Store paths relative to the bundle root: reproducible, and CPython
        # rewrites co_filename to the real location at import time anyway.
        "-s", str(root),
        str(root),
    ]  # fmt: skip
    log.debug(" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Some packages ship intentionally non-compilable files (templates, py2 code).
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("***")]
        log.warn(
            f"compileall reported {len(lines) or 'some'} error(s); those files stay source-only"
        )
        for ln in lines[:5]:
            log.debug(ln)
    return True
