"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, config, log
from .bundle import build_target
from .config import ProjectConfig, TargetConfig
from .errors import ConfigError, PyboxError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    log.set_verbosity(args.verbose - args.quiet)
    try:
        return int(args.func(args))
    except PyboxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-C", "--project", type=Path, default=Path("."), help="project directory (default: .)"
    )
    common.add_argument("-v", "--verbose", action="count", default=0)
    common.add_argument("-q", "--quiet", action="count", default=0)
    adhoc = common.add_argument_group("ad-hoc target (instead of [[tool.pybox.target]])")
    adhoc.add_argument("--platform", help="e.g. linux-x86_64, windows-x86_64, lambda-arm64")
    adhoc.add_argument("--python", help="target Python version (default: [tool.pybox].python)")
    adhoc.add_argument("--glibc", help="newest glibc on the target, e.g. 2.28 (Linux only)")
    adhoc.add_argument("--interpreter", choices=["bundled", "system"])

    p = argparse.ArgumentParser(prog="pybox", description="Build Python application bundles.")
    p.add_argument("--version", action="version", version=f"pybox {__version__}")
    sub = p.add_subparsers(required=True, metavar="COMMAND")

    b = sub.add_parser("build", parents=[common], help="build artifacts")
    b.add_argument("-t", "--target", action="append", default=[], help="target name (repeatable)")
    b.add_argument("-f", "--format", action="append", default=[], help="format (repeatable)")
    b.add_argument("-o", "--out-dir", type=Path, help="output directory (default: <project>/dist)")
    b.add_argument("--lock", type=Path, help="pylock.toml to use")
    b.add_argument(
        "--no-compile",
        dest="compile",
        action="store_false",
        default=None,
        help="do not pre-compile bytecode",
    )
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("targets", parents=[common], help="show resolved targets")
    t.add_argument("--tags", action="store_true", help="also list all compatible wheel tags")
    t.set_defaults(func=cmd_targets)
    return p


def _targets(args: argparse.Namespace, cfg: ProjectConfig) -> list[TargetConfig]:
    if args.platform:
        if getattr(args, "target", None):
            raise ConfigError("--platform cannot be combined with --target")
        return [
            config.make_target_config(
                "adhoc",
                args.platform,
                args.python or cfg.python,
                glibc=args.glibc,
                interpreter=args.interpreter,
                formats=getattr(args, "format", None) or None,
                requires_python=cfg.requires_python,
            )
        ]
    if args.python or args.glibc or args.interpreter:
        raise ConfigError("--python/--glibc/--interpreter require --platform")
    if not cfg.targets:
        raise ConfigError("no targets configured; add [[tool.pybox.target]] or pass --platform")
    names = getattr(args, "target", None)
    return [cfg.target(n) for n in names] if names else list(cfg.targets)


def cmd_build(args: argparse.Namespace) -> int:
    cfg = config.load(args.project)
    out_dir = args.out_dir or cfg.root / "dist"
    outputs: list[Path] = []
    for tc in _targets(args, cfg):
        formats = [f for f in args.format if f in tc.formats] if args.format else None
        if args.format and not formats:
            log.warn(f"target {tc.name!r} has none of the requested formats; skipped")
            continue
        outputs += build_target(
            cfg, tc, out_dir, formats=formats, lock=args.lock, compile_bytecode=args.compile
        )
    for path in outputs:
        print(path)
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    cfg = config.load(args.project)
    for tc in _targets(args, cfg):
        t = tc.target
        print(f"{tc.name}: {t}")
        print(f"  interpreter: {tc.interpreter}")
        print(f"  formats:     {', '.join(tc.formats)}")
        print(f"  platforms:   {', '.join(t.platform_tags())}")
        env = t.marker_environment()
        print("  markers:     " + ", ".join(f"{k}={v!r}" for k, v in env.items()))
        if args.tags:
            for tag in t.tags():
                print(f"    {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
