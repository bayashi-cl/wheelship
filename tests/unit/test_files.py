"""prune, reproducible zip, pip report parsing, wheel installation."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pybox import fetch, prune, reproducible
from pybox.errors import BuildError, FetchError
from pybox.install import flat_scheme, install_wheels
from pybox.lock import Artifact, pipreport
from pybox.target import make_target
from pybox.wheels import Wheel, is_compatible, obtain
from tests.conftest import MakeWheel, sha256


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/tests/**", "pkg/tests/test_a.py", True),
        ("**/tests/**", "tests/x.py", True),
        ("**/tests/**", "pkg/testsuite/x.py", False),
        ("*.pyi", "a.pyi", True),
        ("*.pyi", "pkg/a.pyi", False),
        ("**/*.pyi", "pkg/sub/a.pyi", True),
        ("pkg/?.txt", "pkg/a.txt", True),
        ("pkg/?.txt", "pkg/ab.txt", False),
    ],
)
def test_glob(pattern: str, path: str, expected: bool) -> None:
    assert prune.matches(path, [pattern]) is expected


def test_prune_tree(tmp_path: Path) -> None:
    for rel in ["pkg/__init__.py", "pkg/tests/t.py", "pkg/a.pyi", "keep.txt"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    assert prune.prune(tmp_path, ["**/tests/**", "**/*.pyi"]) == 2
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "keep.txt",
        "pkg",
        "pkg/__init__.py",
    ]


def test_zip_is_deterministic_with_permissions(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (root / "b.py").write_text("b")
    (root / "a.py").write_text("a")
    (root / "bin" / "tool").write_text("#!/bin/sh")
    first, second = tmp_path / "1.zip", tmp_path / "2.zip"
    reproducible.write_zip(root, first, executable=lambda n: n.startswith("bin/"))
    (root / "a.py").touch()  # mtime must not matter
    size = reproducible.write_zip(root, second, executable=lambda n: n.startswith("bin/"))
    assert first.read_bytes() == second.read_bytes()
    assert size == 1 + 1 + len("#!/bin/sh")
    with zipfile.ZipFile(first) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == ["a.py", "b.py", "bin/tool"]
        modes = {i.filename: i.external_attr >> 16 for i in infos}
        assert modes["bin/tool"] == 0o100755
        assert modes["a.py"] == 0o100644
        assert infos[0].date_time == (1980, 1, 1, 0, 0, 0)

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    reproducible.write_zip(root, first)
    with zipfile.ZipFile(first) as zf:
        assert zf.infolist()[0].date_time[0] == 2023


def test_pip_report_parsing() -> None:
    report = {
        "version": "1",
        "install": [
            {
                "metadata": {"name": "Foo_Bar", "version": "1.2"},
                "download_info": {
                    "url": "https://files.example/foo_bar-1.2-py3-none-any.whl",
                    "archive_info": {"hashes": {"sha256": "ab"}},
                },
            },
            {
                "metadata": {"name": "baz", "version": "0.1"},
                "download_info": {
                    "url": "https://files.example/baz-0.1.tar.gz",
                    "archive_info": {"hash": "sha256=cd"},
                },
            },
        ],
    }
    a, b = pipreport.parse_report(report)
    assert (a.name, a.kind, a.filename, dict(a.hashes)) == (
        "foo-bar", "wheel", "foo_bar-1.2-py3-none-any.whl", {"sha256": "ab"}
    )  # fmt: skip
    assert (b.kind, dict(b.hashes)) == ("sdist", {"sha256": "cd"})


def test_pip_command_targets_platform(tmp_path: Path) -> None:
    cmd = pipreport.pip_command(make_target("lambda-arm64", "3.12"), ["attrs"], tmp_path / "r")
    assert "--dry-run" in cmd and "--only-binary=:all:" in cmd
    assert cmd[cmd.index("--python-version") + 1] == "3.12"
    assert cmd[cmd.index("--abi") + 1] == "cp312"
    assert "manylinux_2_34_aarch64" in cmd and cmd[-1] == "attrs"


def test_obtain_verifies_hash_and_tags(tmp_path: Path, make_wheel: MakeWheel) -> None:
    w = make_wheel("foo", "1.0")
    target = make_target("windows-x86_64", "3.12")
    good = Artifact("foo", "1.0", "wheel", path=w, hashes={"sha256": sha256(w)}, filename=w.name)  # type: ignore[arg-type]
    [wheel] = obtain([good], target, tmp_path)
    assert wheel.path == w

    bad = Artifact("foo", "1.0", "wheel", path=w, hashes={"sha256": "00"}, filename=w.name)  # type: ignore[arg-type]
    with pytest.raises(FetchError, match="hash mismatch"):
        obtain([bad], target, tmp_path)

    lin = make_wheel("bar", "1.0", tag="cp312-cp312-manylinux_2_17_x86_64")
    art = Artifact("bar", "1.0", "wheel", path=lin, filename=lin.name)  # type: ignore[arg-type]
    with pytest.raises(BuildError, match="not compatible"):
        obtain([art], target, tmp_path)


def test_obtain_prefers_find_links_for_sdist(tmp_path: Path, make_wheel: MakeWheel) -> None:
    links = tmp_path / "links"
    make_wheel("nat", "1.0", tag="cp312-cp312-win_amd64", out=links)
    make_wheel("nat", "1.0", tag="cp312-cp312-manylinux_2_17_x86_64", out=links)
    sdist = Artifact("nat", "1.0", "sdist", url="https://example.invalid/nat-1.0.tar.gz")  # type: ignore[arg-type]
    [wheel] = obtain([sdist], make_target("lambda-x86_64", "3.12"), tmp_path, find_links=[links])
    assert wheel.path.name == "nat-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"


def test_is_compatible() -> None:
    t = make_target("lambda-x86_64", "3.12")
    assert is_compatible("x-1-cp312-cp312-manylinux_2_34_x86_64.whl", t)
    assert not is_compatible("x-1-cp312-cp312-manylinux_2_35_x86_64.whl", t)
    assert is_compatible("x-1-cp39-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl", t)
    assert not is_compatible("not-a-wheel.zip", t)


def test_install_flat_scheme(tmp_path: Path, make_wheel: MakeWheel) -> None:
    w = make_wheel(
        "tool",
        "1.0",
        files={"tool/__init__.py": "def main(): pass\n"},
        entry_points="[console_scripts]\ntool = tool:main\n",
    )
    root = tmp_path / "stage"
    target = make_target("lambda-x86_64", "3.12")
    art = Artifact("tool", "1.0", "wheel", path=w, filename=w.name)  # type: ignore[arg-type]
    install_wheels([Wheel(art, w)], flat_scheme(root, root), target, interpreter="/py")
    assert (root / "tool" / "__init__.py").is_file()
    info = root / "tool-1.0.dist-info"
    assert (info / "INSTALLER").read_text().startswith("pybox")
    assert "tool/__init__.py" in (info / "RECORD").read_text()
    script = (root / "bin" / "tool").read_text()
    assert script.startswith("#!/py")


def test_pick_hash() -> None:
    assert fetch.pick_hash({"md5": "a", "sha512": "B"}) == ("sha512", "b")
    assert fetch.pick_hash({"blake3": "x"}) is None
