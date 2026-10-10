from __future__ import annotations

import base64
import hashlib
import os
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"

MakeWheel = Callable[..., Path]


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


@pytest.fixture
def make_wheel(tmp_path: Path) -> MakeWheel:
    """Create a minimal but valid wheel file."""

    def make(
        name: str,
        version: str = "1.0",
        tag: str = "py3-none-any",
        files: dict[str, str] | None = None,
        entry_points: str | None = None,
        out: Path | None = None,
    ) -> Path:
        dist = name.replace("-", "_")
        info = f"{dist}-{version}.dist-info"
        contents: dict[str, bytes] = {
            k: v.encode() for k, v in (files or {f"{dist}/__init__.py": ""}).items()
        }
        contents[f"{info}/METADATA"] = (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ).encode()
        contents[f"{info}/WHEEL"] = (
            f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n"
        ).encode()
        if entry_points:
            contents[f"{info}/entry_points.txt"] = entry_points.encode()
        record = [_record_line(k, v) for k, v in contents.items()] + [f"{info}/RECORD,,"]
        contents[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()

        directory = out or tmp_path / "wheels"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{dist}-{version}-{tag}.whl"
        with zipfile.ZipFile(path, "w") as zf:
            for k, v in contents.items():
                zf.writestr(k, v)
        return path

    return make


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("WHEELSHIP_OFFLINE") == "1":
        skip = pytest.mark.skip(reason="WHEELSHIP_OFFLINE=1")
        for item in items:
            if "network" in item.keywords:
                item.add_marker(skip)
