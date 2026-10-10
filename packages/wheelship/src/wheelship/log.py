"""Minimal stderr logging for the CLI."""

from __future__ import annotations

import sys

_verbosity = 0


def set_verbosity(level: int) -> None:
    global _verbosity
    _verbosity = level


def info(msg: str) -> None:
    if _verbosity >= 0:
        print(msg, file=sys.stderr)


def debug(msg: str) -> None:
    if _verbosity >= 1:
        print(f"  {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)
