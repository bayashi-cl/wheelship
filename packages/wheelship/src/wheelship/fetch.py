"""Download distribution files with hash verification and a content-addressed cache."""

from __future__ import annotations

import functools
import hashlib
import os
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import truststore

from . import __version__, log
from .errors import FetchError

_PREFERRED_ALGOS = ("sha256", "sha384", "sha512")
_CHUNK = 1 << 20


def cache_dir() -> Path:
    if env := os.environ.get("WHEELSHIP_CACHE_DIR"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "wheelship" / "cache"
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "wheelship"


def pick_hash(hashes: Mapping[str, str]) -> tuple[str, str] | None:
    for algo in _PREFERRED_ALGOS:
        if algo in hashes:
            return algo, hashes[algo].lower()
    for algo, value in sorted(hashes.items()):
        if algo in hashlib.algorithms_guaranteed:
            return algo, value.lower()
    return None


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def verify(path: Path, hashes: Mapping[str, str], what: str) -> None:
    """Check every hash we know how to compute."""
    for algo, expected in hashes.items():
        if algo not in hashlib.algorithms_guaranteed:
            continue
        actual = file_hash(path, algo)
        if actual != expected.lower():
            raise FetchError(f"hash mismatch for {what}: {algo} expected {expected}, got {actual}")


def fetch(
    url: str,
    filename: str,
    hashes: Mapping[str, str],
    *,
    require_hash: bool = True,
) -> Path:
    """Return a local path to ``url``'s content, downloading into the cache if needed."""
    picked = pick_hash(hashes)
    if picked is None and require_hash:
        raise FetchError(f"no usable hash for {url}; refusing to download unverified file")

    if picked is not None:
        algo, digest = picked
        dest = cache_dir() / "dists" / algo / digest[:2] / digest / filename
        if dest.is_file():
            log.debug(f"cached: {filename}")
            return dest
    else:
        log.warn(f"downloading {url} without hash verification")
        dest = Path(tempfile.mkdtemp(prefix="wheelship-dl-")) / filename

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        _download(url, tmp)
        verify(tmp, hashes, filename)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    log.debug(f"downloaded: {filename}")
    return dest


@functools.cache
def ssl_context() -> ssl.SSLContext:
    # Use the OS trust store: python-build-standalone interpreters (incl. uv's)
    # often cannot locate the system CA bundle through OpenSSL's defaults.
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _download(url: str, dest: Path, *, attempts: int = 3) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"wheelship/{__version__}"})
    for attempt in range(1, attempts + 1):
        try:
            with (
                urllib.request.urlopen(req, timeout=60, context=ssl_context()) as resp,
                dest.open("wb") as out,
            ):
                shutil.copyfileobj(resp, out, _CHUNK)
            return
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == attempts:
                raise FetchError(f"failed to download {url}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == attempts:
                raise FetchError(f"failed to download {url}: {e}") from None
        time.sleep(2 ** (attempt - 1))
