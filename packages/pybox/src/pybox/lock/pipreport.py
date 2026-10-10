"""Fallback resolution through pip's JSON installation report.

Used only when no lock file is given. pip resolves for the *target* via
``--platform/--python-version/--implementation/--abi`` and ``--dry-run``;
the result is not reproducible, which callers must warn about.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from ..errors import ResolveError
from ..target import Target
from . import Artifact


def pip_command(target: Target, requirements: Sequence[str], report: Path) -> list[str]:
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--dry-run", "--ignore-installed", "--quiet",
        "--report", str(report),
        "--only-binary=:all:",
        "--python-version", target.python_minor,
        "--implementation", "cp",
        "--abi", target.python_tag,
    ]  # fmt: skip
    for plat in target.platform_tags():
        cmd += ["--platform", plat]
    return [*cmd, *requirements]


def resolve(target: Target, requirements: Sequence[str]) -> list[Artifact]:
    if not requirements:
        return []
    with tempfile.TemporaryDirectory(prefix="pybox-pip-") as tmp:
        report = Path(tmp) / "report.json"
        cmd = pip_command(target, requirements, report)
        env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        except OSError as e:
            raise ResolveError(f"failed to run pip: {e}") from None
        if proc.returncode != 0:
            if "No module named pip" in proc.stderr:
                raise ResolveError(
                    "no lock file is configured and pip is not available to resolve "
                    "dependencies; create one with `uv export --format pylock.toml --no-dev "
                    "-o pylock.toml` (or `pip lock`) and set [tool.pybox].lock"
                )
            raise ResolveError(f"pip failed to resolve for {target}:\n{proc.stderr.strip()}")
        return parse_report(json.loads(report.read_text(encoding="utf-8")))


def parse_report(data: dict[str, Any]) -> list[Artifact]:
    """Convert a pip installation report (format version 1) into artifacts."""
    if str(data.get("version")) != "1":
        raise ResolveError(f"unsupported pip report version {data.get('version')!r}")
    result: list[Artifact] = []
    for item in data.get("install", []):
        meta = item["metadata"]
        info = item["download_info"]
        url: str = info["url"]
        archive = info.get("archive_info")
        if archive is None:
            raise ResolveError(f"{meta['name']}: unsupported source in pip report: {url}")
        hashes: dict[str, str] = dict(archive.get("hashes") or {})
        if not hashes and archive.get("hash"):
            algo, _, value = archive["hash"].partition("=")
            hashes[algo] = value
        filename = posixpath.basename(urllib.parse.urlsplit(url).path)
        result.append(
            Artifact(
                canonicalize_name(meta["name"]),
                meta["version"],
                "wheel" if filename.endswith(".whl") else "sdist",
                url=url,
                hashes=hashes,
                filename=filename,
            )
        )
    return result
