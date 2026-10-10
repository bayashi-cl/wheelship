"""End-to-end: build the fixture app for Lambda and run the handler from the zip."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from wheelship.bytecode import find_interpreter
from wheelship.cli import main
from wheelship.target import make_target

APP = FIXTURES / "apps" / "lambda_app"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    assert main(["build", "-q", "-C", str(APP), "-o", str(out)]) == 0
    return out


def names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def test_outputs(dist: Path) -> None:
    assert sorted(p.name for p in dist.iterdir()) == [
        "lambda-app-0.1.0-lambda-arm-lambda.zip",
        "lambda-app-0.1.0-lambda-x86-lambda.zip",
        "lambda-app-0.1.0-lambda-x86-layer.zip",
    ]


def test_function_zip_layout(dist: Path) -> None:
    x86 = names(dist / "lambda-app-0.1.0-lambda-x86-lambda.zip")
    assert "lambda_app/handler.py" in x86
    assert "lambda_app/__pycache__/handler.cpython-312.pyc" in x86
    assert "markupsafe/_speedups.cpython-312-x86_64-linux-gnu.so" in x86
    assert any(n.startswith("attrs-") and n.endswith(".dist-info/METADATA") for n in x86)
    assert not any(n.startswith("colorama") for n in x86)
    arm = names(dist / "lambda-app-0.1.0-lambda-arm-lambda.zip")
    assert "markupsafe/_speedups.cpython-312-aarch64-linux-gnu.so" in arm


def test_layer_layout(dist: Path) -> None:
    layer = names(dist / "lambda-app-0.1.0-lambda-x86-layer.zip")
    assert all(n.startswith(("python/", "bin/")) for n in layer)
    assert "python/markupsafe/__init__.py" in layer
    assert not any(n.startswith("python/lambda_app") for n in layer)  # deps only


def test_handler_runs_from_zip(dist: Path, tmp_path: Path) -> None:
    host = make_target("lambda-x86_64", "3.12")
    if not (sys.platform == "linux" and host.arch == "x86_64"):
        pytest.skip("needs an x86_64 Linux host")
    python = find_interpreter(host)
    if python is None:
        pytest.skip("no CPython 3.12 on the host")
    with zipfile.ZipFile(dist / "lambda-app-0.1.0-lambda-x86-lambda.zip") as zf:
        zf.extractall(tmp_path)
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "from lambda_app.handler import handler;"
        "print(json.dumps(handler({'text': '<x>'}, None)))"
    )
    proc = subprocess.run(
        [python, "-I", "-c", code, str(tmp_path)], capture_output=True, text=True, check=True
    )
    result = json.loads(proc.stdout)
    assert result == {
        "escaped": "&lt;x&gt;",
        "native_ext": "_speedups.cpython-312-x86_64-linux-gnu.so",
        "attrs": "26.1.0",
        "python": "3.12",
    }


def test_rebuild_is_byte_identical(dist: Path, tmp_path: Path) -> None:
    assert main(["build", "-q", "-C", str(APP), "-o", str(tmp_path), "-t", "lambda-x86"]) == 0
    for p in tmp_path.iterdir():
        assert p.read_bytes() == (dist / p.name).read_bytes(), p.name
