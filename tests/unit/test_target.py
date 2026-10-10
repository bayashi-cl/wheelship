from __future__ import annotations

import platform
import sys

import pytest
from packaging.markers import Marker
from packaging.tags import Tag, sys_tags

from pybox.errors import ConfigError
from pybox.target import Target, make_target


def test_linux_tags_order_and_bounds() -> None:
    t = make_target("linux-x86_64", "3.12", glibc="2.28")
    tags = [str(x) for x in t.tags()]
    assert tags[0] == "cp312-cp312-manylinux_2_28_x86_64"
    assert "cp312-cp312-manylinux_2_29_x86_64" not in tags  # newer than target glibc
    assert "cp312-cp312-manylinux2014_x86_64" in tags
    assert "cp312-abi3-manylinux1_x86_64" in tags
    assert "cp38-abi3-manylinux_2_17_x86_64" in tags
    assert tags[-1] == "py30-none-any"
    assert not any(tag.endswith("linux_x86_64") and "many" not in tag for tag in tags)
    assert not any("win" in tag or "musl" in tag for tag in tags)


def test_aarch64_has_no_pre_2_17_tags() -> None:
    plats = make_target("linux-aarch64", "3.12").platform_tags()
    assert plats == ["manylinux_2_17_aarch64", "manylinux2014_aarch64"]


def test_windows_tags() -> None:
    t = make_target("windows-x86_64", "3.13")
    tags = t.tags()
    assert tags[0] == Tag("cp313", "cp313", "win_amd64")
    assert {tag.platform for tag in tags} == {"win_amd64", "any"}
    assert t.script_kind == "win-amd64"
    assert make_target("windows-arm64", "3.13").platform_tags() == ["win_arm64"]


@pytest.mark.parametrize(
    ("python", "glibc"), [("3.12", (2, 34)), ("3.13", (2, 34)), ("3.11", (2, 26))]
)
def test_lambda_glibc_follows_runtime(python: str, glibc: tuple[int, int]) -> None:
    t = make_target("lambda-arm64", python)
    assert t.lambda_runtime and t.os == "linux" and t.arch == "aarch64"
    assert t.glibc == glibc
    assert t.platform_name == "lambda-arm64"


def test_marker_environment_is_complete_and_targeted() -> None:
    win = make_target("windows-x86_64", "3.12.7").marker_environment()
    assert win["sys_platform"] == "win32"
    assert win["platform_machine"] == "AMD64"
    assert win["python_full_version"] == "3.12.7"
    assert Marker("sys_platform == 'win32' and python_version >= '3.12'").evaluate(dict(win))
    lin = make_target("linux-aarch64", "3.11").marker_environment()
    assert not Marker("sys_platform == 'win32'").evaluate(dict(lin))
    assert Marker("platform_machine == 'aarch64'").evaluate(dict(lin))
    # every PEP 508 key is present so packaging never falls back to host values
    assert set(win) == set(lin) == {
        "implementation_name", "implementation_version", "os_name", "platform_machine",
        "platform_python_implementation", "platform_release", "platform_system",
        "platform_version", "python_full_version", "python_version", "sys_platform",
    }  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    ["linux", "macos-x86_64", "linux-riscv64", "windows-x86"],
)
def test_invalid_platforms(bad: str) -> None:
    with pytest.raises(ConfigError):
        make_target(bad, "3.12")


def test_invalid_versions() -> None:
    with pytest.raises(ConfigError):
        make_target("linux-x86_64", "3")
    with pytest.raises(ConfigError):
        make_target("linux-x86_64", "3.12", glibc="2")
    with pytest.raises(ConfigError):
        make_target("windows-x86_64", "3.12", glibc="2.28")


def test_ext_suffix() -> None:
    assert make_target("linux-aarch64", "3.12").ext_suffix == ".cpython-312-aarch64-linux-gnu.so"
    assert make_target("windows-x86_64", "3.12").ext_suffix == ".cp312-win_amd64.pyd"


@pytest.mark.skipif(
    not (sys.platform == "linux" and platform.machine() == "x86_64" and platform.libc_ver()[0]),
    reason="golden test needs a glibc x86_64 Linux host",
)
def test_matches_packaging_sys_tags_on_host() -> None:
    """Synthesized tags must equal packaging's host tags (minus non-portable linux_*)."""
    libc = tuple(int(x) for x in platform.libc_ver()[1].split(".")[:2])
    host = Target("linux", "x86_64", (*sys.version_info[:2], 0), glibc=libc)  # type: ignore[arg-type]
    expected = [t for t in sys_tags() if t.platform != "linux_x86_64"]
    assert host.tags() == expected
