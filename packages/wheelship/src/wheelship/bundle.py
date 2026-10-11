"""Build pipeline: resolve → obtain wheels → (per format) install, finalize, package."""

from __future__ import annotations

import dataclasses
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from . import bytecode, launchers, log, pbs, prune, sbom
from .config import ProjectConfig, TargetConfig
from .errors import BuildError, ConfigError, ResolveError
from .lock import Artifact, pipreport, pylock, uv
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
    pbs_asset: pbs.Asset | None = None
    launchers_used: dict[str, Path] = dataclasses.field(default_factory=dict)
    """Launcher kind ("console"/"gui") -> binary placed into an artifact (for the SBOM)."""
    tree_root: Path | None = None
    """Assembled bundle tree, shared by the dir/archive/exe/pyz formats of one target."""

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

    @property
    def artifact_stem(self) -> str:
        return f"{self.cfg.name}-{self.version}-{self.tc.name}"

    def artifact_path(self, suffix: str) -> Path:
        return self.out_dir / f"{self.artifact_stem}-{suffix}"

    def launcher(self, kind: launchers.Kind) -> Path:
        path = launchers.find(self.target, kind)
        self.launchers_used[kind] = path
        return path

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
    from .formats import aws_lambda, package, system

    return {
        "lambda-zip": aws_lambda.build_zip,
        "lambda-layer": aws_lambda.build_layer,
        "lambda-runtime": aws_lambda.build_runtime,
        "dir": package.build_dir,
        "archive": package.build_archive,
        "exe": package.build_exe,
        "pyz": system.build_pyz,
    }


def find_lock(cfg: ProjectConfig, override: Path | None) -> Path | None:
    """A ``pylock.toml``, or a ``uv.lock`` to export one from at build time."""
    if override is not None:
        return override
    if cfg.lock is not None:
        return cfg.lock
    default = cfg.root / "pylock.toml"
    return default if default.is_file() else uv.find(cfg.root)


def resolve(cfg: ProjectConfig, target: Target, lock: Path | None, work: Path) -> list[Artifact]:
    if lock is not None and lock.name == uv.LOCK_NAME:
        shown = lock.name if lock.parent == cfg.root else lock
        log.info(f"resolving from {shown} (uv export) for {target}")
        # extras and groups are already applied by uv (see lock.uv).
        exported, base = uv.export(cfg, work / "uv-export")
        artifacts = pylock.select(exported, target, base=base)
    elif lock is not None:
        log.info(f"resolving from {lock.name} for {target}")
        artifacts = pylock.select(lock, target, extras=cfg.extras, groups=cfg.groups)
    else:
        log.warn(
            "no lock file; resolving with pip for this build only (not reproducible). "
            "Lock with uv (`uv lock`) or create a pylock.toml (`pip lock`)."
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

    asset: pbs.Asset | None = None
    if tc.interpreter == "bundled":
        asset = pbs.resolve(
            cfg.pbs, tc.target.python_spec, pbs.target_triple(tc.target), cfg.pbs_lock
        )
        # The exact patch version is known now: use it for marker evaluation.
        exact = dataclasses.replace(tc.target, python=asset.version, python_exact=True)
        tc = dataclasses.replace(tc, target=exact)
        log.debug(f"python-build-standalone {asset.release}: CPython {tc.target.python_minor}")

    with tempfile.TemporaryDirectory(prefix="wheelship-") as tmp:
        work = Path(tmp)
        artifacts = resolve(cfg, tc.target, find_lock(cfg, lock), work)
        log.info(f"obtaining {len(artifacts)} distributions")
        wheels = obtain(artifacts, tc.target, work, find_links=cfg.find_links)
        ctx = BuildContext(
            cfg=cfg,
            tc=tc,
            out_dir=out_dir.resolve(),
            work=work,
            wheels=wheels,
            compile_bytecode=cfg.compile_bytecode if compile_bytecode is None else compile_bytecode,
            pbs_asset=asset,
        )
        outputs: list[Path] = []
        for fmt in selected:
            log.info(f"building {fmt} for target {tc.name} ({tc.target})")
            try:
                outputs.append(builders[fmt](ctx))
            except OSError as e:
                raise BuildError(f"{fmt}: {e}") from e
        if cfg.sbom:
            outputs.append(sbom.write(ctx))
        return outputs
