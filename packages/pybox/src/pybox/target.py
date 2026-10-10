"""Target environment model.

A :class:`Target` describes the machine a bundle will run on. From it we
*synthesize* the wheel tags (``packaging.tags``) and the PEP 508 marker
environment, never consulting the host. This is what makes cross-building
(e.g. Windows bundles on Linux) possible.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from typing import Literal

from packaging import tags as _tags
from packaging.markers import Environment

from .errors import ConfigError

OS = Literal["linux", "windows"]
Arch = Literal["x86_64", "aarch64"]

# Oldest glibc for which manylinux wheels exist per architecture
# (mirrors packaging._manylinux).
_MIN_GLIBC_MINOR: dict[str, int] = {"x86_64": 5, "aarch64": 17}
_LEGACY_MANYLINUX: dict[tuple[int, int], str] = {
    (2, 17): "manylinux2014",
    (2, 12): "manylinux2010",
    (2, 5): "manylinux1",
}
_DEFAULT_GLIBC = (2, 17)

_WINDOWS_PLATFORM = {"x86_64": "win_amd64", "aarch64": "win_arm64"}
_WINDOWS_MACHINE = {"x86_64": "AMD64", "aarch64": "ARM64"}

_ARCH_ALIASES: dict[str, Arch] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


@dataclasses.dataclass(frozen=True)
class Target:
    os: OS
    arch: Arch
    python: tuple[int, int, int]
    """Target Python version. The patch level only affects marker evaluation."""
    glibc: tuple[int, int] | None = None
    """Newest glibc available on the target (Linux only); bounds manylinux tags."""
    lambda_runtime: bool = False
    """Target is an AWS Lambda managed Python runtime (``/var/task`` layout)."""

    def __post_init__(self) -> None:
        if self.os == "linux" and self.glibc is None:
            object.__setattr__(self, "glibc", _DEFAULT_GLIBC)
        if self.os == "windows" and self.glibc is not None:
            raise ConfigError("glibc is only meaningful for Linux targets")

    # -- identification -------------------------------------------------

    @property
    def python_tag(self) -> str:
        return f"cp{self.python[0]}{self.python[1]}"

    @property
    def python_minor(self) -> str:
        return f"{self.python[0]}.{self.python[1]}"

    @property
    def platform_name(self) -> str:
        if self.lambda_runtime:
            return f"lambda-{'arm64' if self.arch == 'aarch64' else 'x86_64'}"
        return f"{self.os}-{self.arch}"

    def __str__(self) -> str:
        s = f"{self.platform_name}-{self.python_tag}"
        if self.os == "linux" and not self.lambda_runtime:
            assert self.glibc is not None
            s += f"-glibc{self.glibc[0]}.{self.glibc[1]}"
        return s

    # -- wheel tags -----------------------------------------------------

    def platform_tags(self) -> list[str]:
        """Platform tags in priority order (most specific first)."""
        if self.os == "windows":
            return [_WINDOWS_PLATFORM[self.arch]]
        assert self.glibc is not None
        major, newest = self.glibc
        if major != 2:
            raise ConfigError(f"unsupported glibc version {self.glibc}")
        result: list[str] = []
        for minor in range(newest, _MIN_GLIBC_MINOR[self.arch] - 1, -1):
            result.append(f"manylinux_2_{minor}_{self.arch}")
            legacy = _LEGACY_MANYLINUX.get((2, minor))
            if legacy:
                result.append(f"{legacy}_{self.arch}")
        # ``linux_<arch>`` wheels are host-built and not portable: never accepted.
        return result

    def tags(self) -> list[_tags.Tag]:
        """All compatible wheel tags, best first (like ``packaging.tags.sys_tags``)."""
        version = self.python[:2]
        platforms = self.platform_tags()
        return list(
            _chain(
                _tags.cpython_tags(version, abis=[self.python_tag], platforms=platforms),
                _tags.compatible_tags(version, interpreter=self.python_tag, platforms=platforms),
            )
        )

    @property
    def ext_suffix(self) -> str:
        """``sysconfig.get_config_var('EXT_SUFFIX')`` of the target interpreter."""
        v = f"{self.python[0]}{self.python[1]}"
        if self.os == "windows":
            plat = {"x86_64": "win_amd64", "aarch64": "win_arm64"}[self.arch]
            return f".cp{v}-{plat}.pyd"
        return f".cpython-{v}-{self.arch}-linux-gnu.so"

    # -- markers --------------------------------------------------------

    def marker_environment(self) -> Environment:
        """Full PEP 508 marker environment for this target.

        Every key is filled in: ``packaging`` would otherwise substitute host values.
        """
        full = ".".join(map(str, self.python))
        if self.os == "windows":
            os_name, sys_platform, system = "nt", "win32", "Windows"
            machine = _WINDOWS_MACHINE[self.arch]
        else:
            os_name, sys_platform, system = "posix", "linux", "Linux"
            machine = self.arch
        return {
            "implementation_name": "cpython",
            "implementation_version": full,
            "os_name": os_name,
            "platform_machine": machine,
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": system,
            "platform_version": "",
            "python_full_version": full,
            "python_version": self.python_minor,
            "sys_platform": sys_platform,
        }

    # -- installer helpers ----------------------------------------------

    @property
    def script_kind(self) -> Literal["posix", "win-amd64", "win-arm64"]:
        if self.os == "windows":
            return "win-arm64" if self.arch == "aarch64" else "win-amd64"
        return "posix"


def _chain(*its: Iterator[_tags.Tag]) -> Iterator[_tags.Tag]:
    seen: set[_tags.Tag] = set()
    for it in its:
        for tag in it:
            if tag not in seen:
                seen.add(tag)
                yield tag


def parse_python_version(value: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", value.strip())
    if not m:
        raise ConfigError(f"invalid Python version {value!r} (expected e.g. '3.12' or '3.12.7')")
    major, minor, patch = int(m[1]), int(m[2]), int(m[3] or 0)
    if (major, minor) < (3, 9):
        raise ConfigError(f"Python {value} is not supported")
    return major, minor, patch


def parse_glibc(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)", value.strip())
    if not m:
        raise ConfigError(f"invalid glibc version {value!r} (expected e.g. '2.28')")
    return int(m[1]), int(m[2])


def lambda_glibc(python: tuple[int, int, int]) -> tuple[int, int]:
    """glibc of the AWS Lambda managed runtime for a given Python version.

    python3.12+ runs on Amazon Linux 2023 (glibc 2.34); older runtimes on
    Amazon Linux 2 (glibc 2.26).
    """
    return (2, 34) if python[:2] >= (3, 12) else (2, 26)


def make_target(
    platform: str,
    python: str,
    *,
    glibc: str | None = None,
) -> Target:
    """Build a Target from a platform string such as ``linux-x86_64`` or ``lambda-arm64``."""
    try:
        kind, arch_s = platform.strip().lower().split("-", 1)
        arch = _ARCH_ALIASES[arch_s]
    except (ValueError, KeyError):
        raise ConfigError(
            f"invalid platform {platform!r}; expected one of "
            "linux-{x86_64,aarch64}, windows-{x86_64,aarch64}, lambda-{x86_64,arm64}"
        ) from None
    py = parse_python_version(python)
    if kind == "linux":
        return Target("linux", arch, py, parse_glibc(glibc) if glibc else None)
    if kind == "windows":
        if glibc:
            raise ConfigError("glibc cannot be set for a Windows target")
        return Target("windows", arch, py)
    if kind == "lambda":
        g = parse_glibc(glibc) if glibc else lambda_glibc(py)
        return Target("linux", arch, py, g, lambda_runtime=True)
    raise ConfigError(f"unknown platform kind {kind!r} in {platform!r}")
