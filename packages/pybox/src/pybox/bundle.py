"""Build pipeline: resolve → obtain wheels → (per format) install, finalize, package."""

from __future__ import annotations

import dataclasses
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from . import bytecode, log, prune
from .config import ProjectConfig, TargetConfig
from .errors import BuildError, ConfigError, ResolveError
from .lock import Artifact, pipreport, pylock
from .target import Target
from .wheels import Wheel, obtain


@dataclasses.dataclass
class BuildContext:
    cfg: ProjectConfig
    tc: TargetConfig
    out_dir: Path
    work: Path
    wheels: list[Wheel]
    compile_bytecode: bool

    @property
    def target(self) -> Target:
        return self.tc.target

    @property
    def app(self) -> Wheel:
        for w in self.wheels:
            if w.artifact.name == self.cfg.name:
                return w
        raise AssertionError("application wheel missing")  # guaranteed by resolve()

    @property
    def dependencies(self) -> list[Wheel]:
        return [w for w in self.wheels if w.artifact.name != self.cfg.name]

    @property
    def version(self) -> str:
        return self.cfg.version or self.app.version

    def artifact_path(self, suffix: str) -> Path:
        return self.out_dir / f"{self.cfg.name}-{self.version}-{self.tc.name}-{suffix}"

    def stage(self, fmt: str) -> Path:
        path = self.work / "stage" / fmt
        path.mkdir(parents=True)
        return path

    def finalize(self, root: Path) -> None:
        """Steps shared by every format after installation."""
        prune.prune(root, self.cfg.prune)
        if self.compile_bytecode:
            bytecode.compile_tree(root, self.target)


FormatBuilder = Callable[[BuildContext], Path]


def _builders() -> dict[str, FormatBuilder]:
    from .formats import aws_lambda

    return {
        "lambda-zip": aws_lambda.build_zip,
        "lambda-layer": aws_lambda.build_layer,
    }


def find_lock(cfg: ProjectConfig, override: Path | None) -> Path | None:
    if override is not None:
        return override
    if cfg.lock is not None:
        return cfg.lock
    default = cfg.root / "pylock.toml"
    return default if default.is_file() else None


def resolve(cfg: ProjectConfig, target: Target, lock: Path | None) -> list[Artifact]:
    if lock is not None:
        log.info(f"resolving from {lock.name} for {target}")
        artifacts = pylock.select(lock, target, extras=cfg.extras, groups=cfg.groups)
    else:
        log.warn(
            "no lock file; resolving with pip for this build only (not reproducible). "
            "Create one with `uv export --format pylock.toml --no-dev -o pylock.toml`."
        )
        if cfg.dependencies is None:
            raise ResolveError("[project].dependencies is dynamic; a lock file is required")
        reqs = list(cfg.dependencies)
        for extra in cfg.extras:
            if extra not in cfg.optional_dependencies:
                raise ConfigError(f"unknown extra {extra!r}")
            reqs += cfg.optional_dependencies[extra]
        artifacts = pipreport.resolve(target, reqs)

    if not any(a.name == cfg.name for a in artifacts):
        # e.g. `uv export --no-emit-project`, or the pip fallback.
        artifacts.append(Artifact(cfg.name, cfg.version, "directory", path=cfg.root))
    for a in artifacts:
        log.debug(a.describe())
    return artifacts


def build_target(
    cfg: ProjectConfig,
    tc: TargetConfig,
    out_dir: Path,
    *,
    formats: Sequence[str] | None = None,
    lock: Path | None = None,
    compile_bytecode: bool | None = None,
) -> list[Path]:
    builders = _builders()
    selected = list(formats) if formats else list(tc.formats)
    for fmt in selected:
        if fmt not in tc.formats:
            raise ConfigError(f"format {fmt!r} is not configured for target {tc.name!r}")
        if fmt not in builders:
            raise ConfigError(f"format {fmt!r} is not implemented yet")

    artifacts = resolve(cfg, tc.target, find_lock(cfg, lock))
    with tempfile.TemporaryDirectory(prefix="pybox-") as tmp:
        work = Path(tmp)
        log.info(f"obtaining {len(artifacts)} distributions")
        wheels = obtain(artifacts, tc.target, work, find_links=cfg.find_links)
        ctx = BuildContext(
            cfg=cfg,
            tc=tc,
            out_dir=out_dir.resolve(),
            work=work,
            wheels=wheels,
            compile_bytecode=cfg.compile_bytecode if compile_bytecode is None else compile_bytecode,
        )
        outputs: list[Path] = []
        for fmt in selected:
            log.info(f"building {fmt} for target {tc.name} ({tc.target})")
            try:
                outputs.append(builders[fmt](ctx))
            except OSError as e:
                raise BuildError(f"{fmt}: {e}") from e
        return outputs
