"""Experimental: Windows launcher running Python in-process (``WHEELSHIP_EMBED=1``).

Compares the embedded (``Py_Main`` from ``python3XX.dll``) and the default
spawned process models on the same bundle. Timings and probe results go to the
GitHub Actions step summary when available.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from tests.conftest import FIXTURES
from wheelship import launchers, pbs
from wheelship.cli import main
from wheelship.errors import BuildError
from wheelship.target import make_target

APP = FIXTURES / "apps" / "cli_app"
STEM = "cli-app-0.2.0"
pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(pbs.host_triple() != "x86_64-pc-windows-msvc", reason="needs Windows"),
]


def summary(text: str) -> None:
    print(text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def run(exe: Path, *args: str, embed: bool, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(exe), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "WHEELSHIP_EMBED": "1" if embed else "0", **env},
        cwd=exe.parent.parent,
    )


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    try:
        launchers.find(make_target("windows-x86_64", "3.12"), "console")
    except BuildError:
        pytest.skip("no Windows launcher available")
    out = tmp_path_factory.mktemp("dist")
    assert main(["build", "-q", "-C", str(APP), "-t", "windows", "-o", str(out)]) == 0
    with zipfile.ZipFile(out / f"{STEM}-windows.zip") as zf:
        zf.extractall(out / "x")
    return out


@pytest.fixture
def bundle(dist: Path) -> Path:
    return dist / "x" / f"{STEM}-windows"


def test_embedded_single_process(bundle: Path) -> None:
    proc = run(bundle / "cli-app.exe", "--mp", "a", embed=True, WHEELSHIP_DEBUG="1")
    assert proc.returncode == 0, proc.stderr
    assert "wheelship-launcher: embed" in proc.stderr
    info = json.loads(proc.stdout)
    assert info["executable"].lower() == str(bundle / "python" / "python.exe").lower()
    assert info["prefix"].lower() == str(bundle / "python").lower()
    assert info["argv0"].lower() == str(bundle / "cli-app.exe").lower()
    assert info["isolated"] == 1 and info["cwd_in_path"] is False
    assert info["native_ext"] == "_speedups.cp312-win_amd64.pyd"
    assert info["mp"] == [1, 4, 9]  # spawn children run python.exe, not the app
    assert run(bundle / "cli-app.exe", "--exit", "7", embed=True).returncode == 7

    child = subprocess.Popen(
        [str(bundle / "cli-app.exe")],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "WHEELSHIP_EMBED": "1"},
    )
    out, _ = child.communicate()
    assert json.loads(out)["pid"] == child.pid

    child = subprocess.Popen(
        [str(bundle / "cli-app.exe")],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "WHEELSHIP_EMBED": "0"},
    )
    out, _ = child.communicate()
    assert json.loads(out)["pid"] != child.pid


def test_embedded_gui(bundle: Path, tmp_path: Path) -> None:
    report = tmp_path / "gui.json"
    proc = run(bundle / "cli-app-gui.exe", "--write", str(report), "--exit", "3", embed=True)
    assert proc.returncode == 3, proc.stderr
    info = json.loads(report.read_text())
    assert info["executable"].lower().endswith("pythonw.exe")


def test_embedded_onefile(dist: Path, tmp_path: Path) -> None:
    out = tmp_path / "exe"
    assert main(["build", "-q", "-C", str(APP), "-t", "windows", "-f", "exe", "-o", str(out)]) == 0
    cache = tmp_path / "cache"
    proc = run(out / f"{STEM}-windows.exe", "--mp", embed=True, WHEELSHIP_CACHE_DIR=str(cache))
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout)
    assert info["prefix"].lower().startswith(str(cache).lower())
    assert info["mp"] == [1, 4, 9]


def test_probe_pyvenv_cfg(bundle: Path, tmp_path: Path) -> None:
    """A pyvenv.cfg next to (or above) the executable: CPython's getpath reads it
    for the *process* executable, which is the launcher when embedding."""
    rows = ["| pyvenv.cfg | mode | exit | prefix / error |", "|---|---|---|---|"]
    for where in ("beside", "parent"):
        copy = tmp_path / where / "app"
        shutil.copytree(bundle, copy)
        cfg = copy / "pyvenv.cfg" if where == "beside" else copy.parent / "pyvenv.cfg"
        cfg.write_text("home = C:\\nowhere\ninclude-system-site-packages = false\n")
        for embed in (False, True):
            proc = run(copy / "cli-app.exe", embed=embed)
            try:
                detail = json.loads(proc.stdout)["prefix"]
            except ValueError:
                detail = " ".join(proc.stderr.strip().splitlines()[-1:])[:120]
            mode = "embed" if embed else "spawn"
            rows.append(f"| {where} | {mode} | {proc.returncode} | `{detail}` |")
    summary("\n### Embedding probe: pyvenv.cfg\n\n" + "\n".join(rows) + "\n")


def test_benchmark_startup(bundle: Path) -> None:
    exe = bundle / "cli-app.exe"
    rows = ["| mode | median ms | min ms |", "|---|---|---|"]
    for embed in (False, True):
        run(exe, embed=embed)  # warm the file cache
        times = []
        for _ in range(15):
            start = time.perf_counter()
            assert run(exe, embed=embed).returncode == 0
            times.append((time.perf_counter() - start) * 1000)
        mode = "embed" if embed else "spawn"
        rows.append(f"| {mode} | {statistics.median(times):.1f} | {min(times):.1f} |")
    summary("\n### Embedding benchmark: launcher start-up (cli-app, no args)\n\n"
            + "\n".join(rows) + "\n")  # fmt: skip
