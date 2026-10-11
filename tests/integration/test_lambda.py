"""End-to-end: build the fixture app for Lambda and run the handler from the zip."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
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
        "lambda-app-0.1.0-lambda-arm.cdx.json",
        "lambda-app-0.1.0-lambda-custom-lambda-runtime.zip",
        "lambda-app-0.1.0-lambda-custom.cdx.json",
        "lambda-app-0.1.0-lambda-x86-lambda.zip",
        "lambda-app-0.1.0-lambda-x86-layer.zip",
        "lambda-app-0.1.0-lambda-x86.cdx.json",
    ]


def test_sbom(dist: Path) -> None:
    doc = json.loads((dist / "lambda-app-0.1.0-lambda-custom.cdx.json").read_text())
    assert doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.6"
    assert doc["metadata"]["component"]["purl"] == "pkg:pypi/lambda-app@0.1.0"
    by_name = {c["name"]: c for c in doc["components"]}
    assert by_name["attrs"]["purl"] == "pkg:pypi/attrs@26.1.0"
    assert by_name["attrs"]["licenses"] == [{"expression": "MIT"}]
    assert len(by_name["MarkupSafe"]["hashes"][0]["content"]) == 64
    assert by_name["cpython"]["version"].startswith("3.14.")
    assert "colorama" not in by_name  # win32-only dependency
    managed = json.loads((dist / "lambda-app-0.1.0-lambda-x86.cdx.json").read_text())
    assert "cpython" not in {c["name"] for c in managed["components"]}


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


def test_runtime_layout(dist: Path) -> None:
    zf = zipfile.ZipFile(dist / "lambda-app-0.1.0-lambda-custom-lambda-runtime.zip")
    infos = {i.filename: i for i in zf.infolist()}
    assert infos["bootstrap"].external_attr >> 16 == 0o100755
    assert infos["python/bin/python3.14"].external_attr >> 16 == 0o100755
    assert "python/bin/python3" not in infos  # alias dropped instead of duplicated
    site = "python/lib/python3.14/site-packages/"
    assert site + "lambda_app/handler.py" in infos
    assert site + "awslambdaric/__main__.py" in infos
    assert not any(n.startswith("python/lib/python3.14/tkinter/") for n in infos)
    assert b"-m awslambdaric" in zf.read("bootstrap")


# -- real Lambda environments: AWS base images with the Runtime Interface Emulator --


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    # Windows runners have docker in Windows-container mode: Lambda images need Linux.
    proc = subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True
    )
    return proc.returncode == 0 and proc.stdout.strip() == "linux"


def _extract_with_modes(archive: Path, dest: Path) -> None:
    """Unzip like Lambda does: keep the Unix permissions stored in the archive."""
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            path = zf.extract(info, dest)
            os.chmod(path, (info.external_attr >> 16) & 0o777 or 0o644)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _invoke_in_docker(task: Path, image: str, run_args: list[str], event: dict) -> dict:  # type: ignore[type-arg]
    port = _free_port()
    name = f"wheelship-test-{port}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8080",
         "-v", f"{task}:/var/task:ro", *run_args[:-1], image, *run_args[-1:]],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        url = f"http://127.0.0.1:{port}/2015-03-31/functions/function/invocations"
        deadline = time.monotonic() + 60
        while True:
            try:
                req = urllib.request.Request(url, data=json.dumps(event).encode())
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())  # type: ignore[no-any-return]
            except OSError:
                if time.monotonic() > deadline:
                    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
                    raise AssertionError(logs.stdout + logs.stderr) from None
                time.sleep(0.5)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


docker = pytest.mark.skipif(not _docker_ok(), reason="needs a working docker")


@docker
def test_function_zip_on_managed_runtime(dist: Path, tmp_path: Path) -> None:
    _extract_with_modes(dist / "lambda-app-0.1.0-lambda-x86-lambda.zip", tmp_path)
    result = _invoke_in_docker(
        tmp_path, "public.ecr.aws/lambda/python:3.12", ["lambda_app.handler.handler"],
        {"text": "<managed>"},
    )  # fmt: skip
    assert result["escaped"] == "&lt;managed&gt;"
    assert result["native_ext"] == "_speedups.cpython-312-x86_64-linux-gnu.so"
    assert result["python"] == "3.12"


@docker
def test_custom_runtime_on_provided_al2023(dist: Path, tmp_path: Path) -> None:
    _extract_with_modes(dist / "lambda-app-0.1.0-lambda-custom-lambda-runtime.zip", tmp_path)
    result = _invoke_in_docker(
        tmp_path,
        "public.ecr.aws/lambda/provided:al2023",
        # Run the emulator directly on /var/task/bootstrap, as Lambda does for zips.
        ["--entrypoint", "/usr/local/bin/aws-lambda-rie",
         "-e", "_HANDLER=lambda_app.handler.handler", "/var/task/bootstrap"],
        {"text": "<custom>"},
    )  # fmt: skip
    assert result["escaped"] == "&lt;custom&gt;"
    assert result["native_ext"] == "_speedups.cpython-314-x86_64-linux-gnu.so"
    assert result["python"] == "3.14"
