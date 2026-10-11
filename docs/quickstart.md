# Quick Start

This guide packages a small command line application into a single Linux executable
and a portable archive that run without Python installed.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Until wheelship is released on PyPI: git and a [Rust toolchain](https://rustup.rs/)
  to build the native launcher

## Install wheelship

=== "From source (current)"

    wheelship is not on PyPI yet. Build the launcher and install the tool in editable
    mode, so that it finds the launcher in the repository's `target/` directory:

    ```sh
    git clone https://github.com/bayashi-cl/wheelship.git
    cd wheelship
    cargo build --release
    uv tool install -e packages/wheelship
    ```

    !!! note
        `cargo build` produces a launcher for the host only, so a source install can
        build `dir`, `archive` and `exe` for the host platform. Formats without a
        launcher (`pyz`, `lambda-zip`, `lambda-layer`) work for any target. To
        cross-build Windows bundles from Linux (or vice versa), point
        `WHEELSHIP_LAUNCHER_DIR` at the `launcher-*` artifacts from CI.

=== "From PyPI (after the first release)"

    ```sh
    uv tool install wheelship
    ```

    Release wheels carry prebuilt launchers for every supported target.

Check the installation:

```sh
wheelship --version
```

## Create a project

Any project with `[project.scripts]` (or `gui-scripts`) works. Start a new one:

```sh
uv init --package --python 3.13 hello
cd hello
uv add rich
```

Replace `src/hello/__init__.py` with:

```python
import sys

from rich import print


def main() -> None:
    print(f"[bold green]Hello[/] from Python {sys.version.split()[0]} at {sys.executable}")
```

`uv init --package` already declared the entry point in `pyproject.toml`:

```toml
[project.scripts]
hello = "hello:main"
```

## Configure targets

Append to `pyproject.toml`:

```toml
[tool.wheelship]
python = "3.13"            # must satisfy requires-python

[[tool.wheelship.target]]
name = "linux"
platform = "linux-x86_64"
formats = ["exe", "archive"]
```

Each `[[tool.wheelship.target]]` is one platform to build for. The interpreter defaults
to `bundled` (python-build-standalone).

## Lock dependencies

wheelship installs exactly what the lock file lists. `uv add` has already written
`uv.lock`; at build time wheelship exports it to a [PEP 751](https://peps.python.org/pep-0751/)
lock with `uv export --format pylock.toml --locked --no-default-groups`, so there is
nothing to generate or commit besides `uv.lock`. `--locked` makes the build fail if
`uv.lock` is out of date with `pyproject.toml`; run `uv lock` after editing dependencies.

In a uv workspace, run wheelship on the member (`wheelship build -C packages/app`):
`uv.lock` is found at the workspace root and uv exports only that member and its
dependencies, sibling members included.

??? note "Lock options"
    - `[tool.wheelship] extras` and `groups` become `--extra` / `--group`.
    - `[tool.wheelship] uv-export-args` appends arguments, e.g.
      `["--no-emit-package", "foo"]`. `--frozen` there replaces `--locked`.
    - Without uv, or to control the export completely, write the lock yourself
      (`pip lock`, `uv export ... -o pylock.toml`) and set `[tool.wheelship] lock`
      or pass `--lock`. A `pylock.toml` next to `pyproject.toml` takes precedence over
      `uv.lock`.

To see what the target resolves to
(wheel tags and environment markers):

```console
$ wheelship targets
linux: linux-x86_64-cp313-glibc2.17
  interpreter: bundled
  formats:     exe, archive
  ...
```

## Build

```console
$ wheelship build
pinned python-build-standalone 20261009 in wheelship.lock
resolving from uv.lock (uv export) for linux-x86_64-cp313-glibc2.17
obtaining 5 distributions
building exe for target linux (linux-x86_64-cp313-glibc2.17)
  python-build-standalone: cpython-3.13.16+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
  hello-0.1.0-linux.bin: 30.6 MB (78.9 MB unpacked)
building archive for target linux (linux-x86_64-cp313-glibc2.17)
  hello-0.1.0-linux.tar.gz: 27.7 MB (79.4 MB unpacked)
```

`dist/` now contains:

| file | what it is |
|---|---|
| `hello-0.1.0-linux.bin` | single executable; extracts itself once into a per-user cache |
| `hello-0.1.0-linux.tar.gz` | relocatable directory with a `hello` launcher, `python/` and `wheelship.json` |
| `hello-0.1.0-linux.cdx.json` | CycloneDX SBOM of everything inside |

The first build also writes `wheelship.lock`, which pins the python-build-standalone
release. Commit it together with `uv.lock` for reproducible builds.

## Run

```console
$ ./dist/hello-0.1.0-linux.bin
Hello from Python 3.13.16 at ~/.cache/wheelship/apps/hello/2878c061f903b905/python/bin/python3.13
```

The archive runs from wherever it is unpacked:

```console
$ tar xzf dist/hello-0.1.0-linux.tar.gz -C /opt
$ /opt/hello-0.1.0-linux/hello
Hello from Python 3.13.16 at /opt/hello-0.1.0-linux/python/bin/python3.13
```

Set `WHEELSHIP_DEBUG=1` to see what the launcher executes.

## Next steps

### Add Windows

Add another target; wheelship downloads Windows wheels and python-build-standalone
even when building on Linux:

```toml
[[tool.wheelship.target]]
name = "windows"
platform = "windows-x86_64"
formats = ["exe", "archive"]
```

Build one target only with `wheelship build -t windows`.

### Use the system Python

`interpreter = "system"` skips python-build-standalone and runs on a CPython of the
target's version found on `PATH` (or `WHEELSHIP_PYTHON`). This also enables the `pyz`
format (a PEP 441 zipapp):

```toml
[[tool.wheelship.target]]
name = "linux-system"
platform = "linux-x86_64"
interpreter = "system"
formats = ["pyz"]
```

### Deploy to AWS Lambda

Lambda targets use presets for the runtime's platform and glibc. An ad-hoc build needs
no configuration:

```sh
wheelship build --platform lambda-arm64 -f lambda-zip
```

`dist/hello-0.1.0-adhoc-lambda.zip` is a deployment package for the managed Python
runtime. See [the output formats](index.md#output-formats) for layers and custom
runtimes (`lambda-runtime`, which needs `[tool.wheelship.lambda] handler`).
