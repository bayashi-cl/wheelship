from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from pybox import config
from pybox.errors import ConfigError

BASE = """
[project]
name = "My_App"
version = "1.0"
requires-python = ">=3.11"
dependencies = ["attrs"]
[project.optional-dependencies]
fast = ["orjson"]
"""


def parse(extra: str, tmp_path: Path) -> config.ProjectConfig:
    data: dict[str, Any] = tomllib.loads(BASE + extra)
    return config.parse(data, tmp_path)


def test_full_config(tmp_path: Path) -> None:
    cfg = parse(
        """
[tool.pybox]
lock = "pylock.toml"
python = "3.12"
prune = ["**/tests/**"]
[[tool.pybox.target]]
name = "fn"
platform = "lambda-arm64"
formats = ["lambda-zip", "lambda-layer"]
[[tool.pybox.target]]
name = "old"
platform = "lambda-x86_64"
python = "3.11"
[tool.pybox.lambda]
handler = "my_app.h:handler"
""",
        tmp_path,
    )
    assert cfg.name == "my-app"
    assert cfg.lock == tmp_path / "pylock.toml"
    assert cfg.dependencies == ("attrs",)
    assert cfg.optional_dependencies == {"fast": ("orjson",)}
    fn = cfg.target("fn")
    assert fn.interpreter == "system"  # lambda default
    assert fn.target.python[:2] == (3, 12)
    old = cfg.target("old")
    assert old.formats == ("lambda-zip",)
    assert old.target.glibc == (2, 26)
    assert cfg.lambda_.handler == "my_app.h:handler"
    with pytest.raises(ConfigError, match="unknown target"):
        cfg.target("nope")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("[tool.pybox]\npyhton = '3.12'", "unknown key"),
        ("[[tool.pybox.target]]\nname='a'\nplatform='lambda-x86_64'", "Python version is not set"),
        (
            "[tool.pybox]\npython='3.10'\n[[tool.pybox.target]]\nname='a'\nplatform='lambda-x86_64'",
            "requires-python",
        ),
        (
            "[tool.pybox]\npython='3.12'\n[[tool.pybox.target]]\nname='a'\n"
            "platform='linux-x86_64'\nformats=['lambda-zip']",
            "requires a lambda",
        ),
        (
            "[tool.pybox]\npython='3.12'\n[[tool.pybox.target]]\nname='a'\n"
            "platform='lambda-x86_64'\ninterpreter='bundled'",
            "interpreter = 'system'",
        ),
        (
            "[tool.pybox]\npython='3.12'\n[[tool.pybox.target]]\nname='a'\n"
            "platform='lambda-x86_64'\nformats=['zipapp']",
            "unknown format",
        ),
        (
            "[tool.pybox]\npython='3.12'\n[[tool.pybox.target]]\nname='a'\n"
            "platform='linux-x86_64'\nformats=['exe']",
            "not implemented",
        ),
        (
            "[tool.pybox]\npython='3.12'\n[[tool.pybox.target]]\nname='a'\nplatform='lambda-arm64'"
            "\n[[tool.pybox.target]]\nname='a'\nplatform='lambda-x86_64'",
            "duplicate",
        ),
    ],
)
def test_errors(extra: str, message: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(extra, tmp_path)


def test_dynamic_dependencies(tmp_path: Path) -> None:
    data = tomllib.loads('[project]\nname="x"\ndynamic=["dependencies", "version"]')
    cfg = config.parse(data, tmp_path)
    assert cfg.dependencies is None
    assert cfg.version is None


def test_load_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        config.load(tmp_path)
