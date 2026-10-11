"""Load ``pyproject.toml`` (``[project]`` + ``[tool.wheelship]``)."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name

from .errors import ConfigError
from .pbs import FLAVORS, STRIP_GROUPS, PbsConfig
from .target import Target, make_target

Interpreter = Literal["bundled", "system"]

FORMATS = ("dir", "archive", "exe", "pyz", "lambda-zip", "lambda-layer", "lambda-runtime")


@dataclasses.dataclass(frozen=True)
class TargetConfig:
    name: str
    target: Target
    interpreter: Interpreter
    formats: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class LambdaConfig:
    handler: str | None = None
    layer_include_app: bool = False
    """Also put the application itself into ``lambda-layer`` (default: dependencies only)."""


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    root: Path
    name: NormalizedName
    version: str | None
    requires_python: SpecifierSet | None
    dependencies: tuple[str, ...] | None
    """``[project].dependencies``; None when declared dynamic."""
    optional_dependencies: dict[str, tuple[str, ...]]
    lock: Path | None
    python: str | None
    extras: tuple[str, ...]
    groups: tuple[str, ...] | None
    uv_export_args: tuple[str, ...]
    """Extra arguments for ``uv export`` when the lock is derived from ``uv.lock``."""
    compile_bytecode: bool
    sbom: bool
    python_flags: tuple[str, ...]
    prune: tuple[str, ...]
    find_links: tuple[Path, ...]
    targets: tuple[TargetConfig, ...]
    lambda_: LambdaConfig
    pbs: PbsConfig

    @property
    def pbs_lock(self) -> Path:
        return self.root / "wheelship.lock"

    def target(self, name: str) -> TargetConfig:
        for t in self.targets:
            if t.name == name:
                return t
        known = ", ".join(t.name for t in self.targets) or "(none)"
        raise ConfigError(f"unknown target {name!r}; configured targets: {known}")


_TOOL_KEYS = {
    "lock", "python", "interpreter", "extras", "groups", "compile-bytecode",
    "prune", "find-links", "target", "lambda", "python-flags", "pbs", "sbom",
    "uv-export-args",
}  # fmt: skip
_TARGET_KEYS = {"name", "platform", "python", "glibc", "interpreter", "formats"}
_LAMBDA_KEYS = {"handler", "layer-include-app"}
_PBS_KEYS = {"release", "flavor", "strip"}


def load(project_dir: Path) -> ProjectConfig:
    root = project_dir.resolve()
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data, root)


def parse(data: dict[str, Any], root: Path) -> ProjectConfig:
    project = _table(data, "project", "pyproject.toml")
    if "name" not in project:
        raise ConfigError("[project].name is required")
    name = canonicalize_name(_str(project["name"], "project.name"))
    version = project.get("version")
    if version is not None:
        version = _str(version, "project.version")

    requires_python: SpecifierSet | None = None
    if "requires-python" in project:
        try:
            requires_python = SpecifierSet(_str(project["requires-python"], "requires-python"))
        except InvalidSpecifier as e:
            raise ConfigError(f"invalid [project].requires-python: {e}") from None

    dynamic = set(_strs(project.get("dynamic", []), "project.dynamic"))
    dependencies = (
        None
        if "dependencies" in dynamic
        else tuple(_strs(project.get("dependencies", []), "project.dependencies"))
    )
    optional_table = _table(project, "optional-dependencies", "project", required=False)
    optional = {
        extra: tuple(_strs(reqs, f"project.optional-dependencies.{extra}"))
        for extra, reqs in optional_table.items()
    }

    tool = _table(data.get("tool", {}), "wheelship", "tool", required=False)
    _check_keys(tool, _TOOL_KEYS, "[tool.wheelship]")

    python = tool.get("python")
    if python is not None:
        python = _str(python, "tool.wheelship.python")
    default_interp = _interpreter(tool.get("interpreter", "bundled"), "tool.wheelship.interpreter")

    lam = _table(tool, "lambda", "tool.wheelship", required=False)
    _check_keys(lam, _LAMBDA_KEYS, "[tool.wheelship.lambda]")
    lambda_cfg = LambdaConfig(
        handler=_str(lam["handler"], "lambda.handler") if "handler" in lam else None,
        layer_include_app=bool(lam.get("layer-include-app", False)),
    )

    targets = tuple(
        _parse_target(t, i, python, default_interp, requires_python)
        for i, t in enumerate(_list(tool.get("target", []), "tool.wheelship.target"))
    )
    names = [t.name for t in targets]
    if len(set(names)) != len(names):
        raise ConfigError(f"duplicate target names in [[tool.wheelship.target]]: {names}")

    groups = tool.get("groups")
    return ProjectConfig(
        root=root,
        name=name,
        version=version,
        requires_python=requires_python,
        dependencies=dependencies,
        optional_dependencies=optional,
        lock=(root / _str(tool["lock"], "tool.wheelship.lock")) if "lock" in tool else None,
        python=python,
        extras=tuple(_strs(tool.get("extras", []), "tool.wheelship.extras")),
        groups=None if groups is None else tuple(_strs(groups, "tool.wheelship.groups")),
        uv_export_args=tuple(_strs(tool.get("uv-export-args", []), "uv-export-args")),
        compile_bytecode=bool(tool.get("compile-bytecode", True)),
        sbom=bool(tool.get("sbom", True)),
        python_flags=tuple(_strs(tool.get("python-flags", ["-I"]), "tool.wheelship.python-flags")),
        prune=tuple(_strs(tool.get("prune", []), "tool.wheelship.prune")),
        find_links=tuple(root / p for p in _strs(tool.get("find-links", []), "find-links")),
        targets=targets,
        lambda_=lambda_cfg,
        pbs=_parse_pbs(_table(tool, "pbs", "tool.wheelship", required=False)),
    )


def _parse_pbs(raw: dict[str, Any]) -> PbsConfig:
    _check_keys(raw, _PBS_KEYS, "[tool.wheelship.pbs]")
    cfg = PbsConfig()
    release = raw.get("release")
    if release is not None and not (isinstance(release, str) and release.isdigit()):
        raise ConfigError("[tool.wheelship.pbs].release must be a release tag such as '20261009'")
    flavor = raw.get("flavor", cfg.flavor)
    if flavor not in FLAVORS:
        raise ConfigError(f"[tool.wheelship.pbs].flavor must be one of {', '.join(FLAVORS)}")
    strip = tuple(_strs(raw.get("strip", list(cfg.strip)), "tool.wheelship.pbs.strip"))
    for group in strip:
        if group not in STRIP_GROUPS:
            raise ConfigError(
                f"unknown [tool.wheelship.pbs].strip group {group!r} "
                f"(known: {', '.join(STRIP_GROUPS)})"
            )
    return PbsConfig(release=release, flavor=flavor, strip=strip)


def make_target_config(
    name: str,
    platform: str,
    python: str | None,
    *,
    glibc: str | None = None,
    interpreter: str | None = None,
    formats: list[str] | None = None,
    default_interpreter: Interpreter = "bundled",
    requires_python: SpecifierSet | None = None,
) -> TargetConfig:
    if python is None:
        raise ConfigError(
            f"target {name!r}: Python version is not set "
            "(set [tool.wheelship].python or the target's python)"
        )
    target = make_target(platform, python, glibc=glibc)
    version = ".".join(map(str, target.python))
    if requires_python is not None and not requires_python.contains(version, prereleases=True):
        raise ConfigError(
            f"target {name!r}: Python {python} does not satisfy "
            f"requires-python {str(requires_python)!r}"
        )
    if interpreter is not None:
        interp: Interpreter = _interpreter(interpreter, f"target {name!r} interpreter")
    elif target.lambda_runtime:
        # Managed runtimes provide the interpreter; a custom runtime bundles one.
        interp = "bundled" if formats and "lambda-runtime" in formats else "system"
    else:
        interp = default_interpreter
    if formats is None:
        formats = ["lambda-zip"] if target.lambda_runtime else ["dir"]
    if target.lambda_runtime and interp == "bundled" and glibc is None:
        # Custom runtimes run on provided.al2023 (glibc 2.34) whatever the Python.
        target = dataclasses.replace(target, glibc=(2, 34))
    for fmt in formats:
        _check_format(fmt, target, interp, name)
    return TargetConfig(name, target, interp, tuple(formats))


def _parse_target(
    raw: Any,
    index: int,
    python: str | None,
    default_interp: Interpreter,
    requires_python: SpecifierSet | None,
) -> TargetConfig:
    where = f"[[tool.wheelship.target]] #{index + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    _check_keys(raw, _TARGET_KEYS, where)
    for key in ("name", "platform"):
        if key not in raw:
            raise ConfigError(f"{where}: '{key}' is required")
    return make_target_config(
        _str(raw["name"], f"{where}.name"),
        _str(raw["platform"], f"{where}.platform"),
        _str(raw["python"], f"{where}.python") if "python" in raw else python,
        glibc=_str(raw["glibc"], f"{where}.glibc") if "glibc" in raw else None,
        interpreter=raw.get("interpreter"),
        formats=_strs(raw["formats"], f"{where}.formats") if "formats" in raw else None,
        default_interpreter=default_interp,
        requires_python=requires_python,
    )


def _check_format(fmt: str, target: Target, interp: Interpreter, name: str) -> None:
    if fmt not in FORMATS:
        raise ConfigError(f"target {name!r}: unknown format {fmt!r} (known: {', '.join(FORMATS)})")
    if fmt in ("dir", "archive", "exe", "pyz") and target.lambda_runtime:
        raise ConfigError(f"target {name!r}: format {fmt!r} is not for Lambda targets")
    if fmt == "pyz" and interp != "system":
        raise ConfigError(
            f"target {name!r}: 'pyz' runs on the target's interpreter; set interpreter = 'system'"
        )
    if fmt == "lambda-runtime":
        if not target.lambda_runtime:
            raise ConfigError(f"target {name!r}: 'lambda-runtime' requires a lambda-* platform")
        if interp != "bundled":
            raise ConfigError(
                f"target {name!r}: 'lambda-runtime' bundles the interpreter; "
                "set interpreter = 'bundled'"
            )
    if fmt in ("lambda-zip", "lambda-layer"):
        if not target.lambda_runtime:
            raise ConfigError(f"target {name!r}: format {fmt!r} requires a lambda-* platform")
        if interp != "system":
            raise ConfigError(
                f"target {name!r}: {fmt!r} uses the managed runtime's interpreter; "
                "set interpreter = 'system' (or use 'lambda-runtime')"
            )


# -- small validation helpers ---------------------------------------------


def _table(d: Any, key: str, where: str, *, required: bool = True) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise ConfigError(f"[{where}] must be a table")
    if key not in d:
        if required:
            raise ConfigError(f"[{key}] table is missing in {where}")
        return {}
    v = d[key]
    if not isinstance(v, dict):
        raise ConfigError(f"[{where}.{key}] must be a table")
    return v


def _check_keys(d: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _str(v: Any, where: str) -> str:
    if not isinstance(v, str):
        raise ConfigError(f"{where} must be a string")
    return v


def _list(v: Any, where: str) -> list[Any]:
    if not isinstance(v, list):
        raise ConfigError(f"{where} must be an array")
    return v


def _strs(v: Any, where: str) -> list[str]:
    return [_str(x, where) for x in _list(v, where)]


def _interpreter(v: Any, where: str) -> Interpreter:
    if v not in ("bundled", "system"):
        raise ConfigError(f"{where} must be 'bundled' or 'system', not {v!r}")
    return v  # type: ignore[no-any-return]
