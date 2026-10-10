"""Phase 3: lambda-runtime configuration and SBOM details."""

from __future__ import annotations

import email.parser

import pytest

from wheelship import config, sbom
from wheelship.errors import ConfigError


def test_lambda_runtime_defaults_to_bundled_on_al2023() -> None:
    tc = config.make_target_config("fn", "lambda-arm64", "3.11", formats=["lambda-runtime"])
    assert tc.interpreter == "bundled"
    assert tc.target.glibc == (2, 34)  # provided.al2023, not the python3.11 managed runtime
    managed = config.make_target_config("fn", "lambda-arm64", "3.11", formats=["lambda-zip"])
    assert managed.interpreter == "system" and managed.target.glibc == (2, 26)


@pytest.mark.parametrize(
    ("platform", "interpreter", "formats", "message"),
    [
        ("linux-x86_64", None, ["lambda-runtime"], "requires a lambda"),
        ("lambda-x86_64", "system", ["lambda-runtime"], "set interpreter = 'bundled'"),
        ("lambda-x86_64", None, ["lambda-runtime", "lambda-zip"], "interpreter = 'system'"),
    ],
)
def test_lambda_runtime_rules(
    platform: str, interpreter: str | None, formats: list[str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config.make_target_config("fn", platform, "3.12", interpreter=interpreter, formats=formats)


def _meta(text: str) -> email.message.Message:
    return email.parser.Parser().parsestr(text)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ("License-Expression: MIT OR Apache-2.0\nLicense: ignored\n",
         [{"expression": "MIT OR Apache-2.0"}]),
        ("Classifier: License :: OSI Approved :: BSD License\n"
         "Classifier: License :: OSI Approved :: BSD License\n",
         [{"license": {"name": "BSD License"}}]),
        ("License: PSF\n", [{"license": {"name": "PSF"}}]),
        ("License: Permission is hereby granted, free of charge,\n to any person...\n", []),
        ("Name: x\n", []),
    ],
)  # fmt: skip
def test_sbom_licenses(metadata: str, expected: list[dict[str, object]]) -> None:
    assert sbom._licenses(_meta(metadata)) == expected
