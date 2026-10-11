# wheelship

Build relocatable Python application bundles for Linux, Windows and AWS Lambda.

wheelship takes your project and its locked dependencies (`pylock.toml`, PEP 751),
installs the wheels for each **target** platform — cross-building from any host — and
packages the result together with an interpreter:

- **bundled**: ships [python-build-standalone](https://github.com/astral-sh/python-build-standalone),
  so users need nothing installed;
- **system**: uses a CPython of the target's version found on `PATH`.

Wheels are installed as-is with standard tools (`installer`, `packaging`); there is no
import hooking or freezing, so `importlib.metadata`, `importlib.resources`, native
extensions, `multiprocessing` and `subprocess [sys.executable, ...]` behave as in a venv.

New here? Start with the [Quick Start](quickstart.md).

## Output formats

| format | interpreter | output |
|---|---|---|
| `dir` / `archive` | bundled or system | directory / `.tar.gz` (Linux) or `.zip` (Windows) |
| `exe` | bundled or system | single file (`.exe` / `.bin`), extracted once into a cache |
| `pyz` | system | PEP 441 zipapp |
| `lambda-zip` / `lambda-layer` | Lambda managed runtime | deployment package / layer |
| `lambda-runtime` | bundled (`provided.al2023`) | custom runtime: `bootstrap` + python-build-standalone + awslambdaric |

Every target also gets a CycloneDX SBOM (`<name>-<version>-<target>.cdx.json`).
The python-build-standalone release is pinned in `wheelship.lock` (commit it).

## CLI

```sh
wheelship targets                                            # show tags / markers per target
wheelship build                                              # all configured targets -> dist/
wheelship build -t linux -f archive                          # one target, one format
wheelship build --platform lambda-arm64 --python 3.13 -f lambda-zip   # ad-hoc target
```

## Environment variables

Runtime (read by the built application):

| variable | effect |
|---|---|
| `WHEELSHIP_DEBUG=1` | show what is executed |
| `WHEELSHIP_BOOT=python` | run the interpreter directly |
| `WHEELSHIP_PYTHON` | interpreter for system bundles |
| `WHEELSHIP_CACHE_DIR` | where `exe` / `pyz` extract |
| `WHEELSHIP_EMBED=1` | experimental, Windows bundled: run Python inside the launcher process |

Build time: `WHEELSHIP_CACHE_DIR`, `WHEELSHIP_PBS_MIRROR`, `WHEELSHIP_LAUNCHER_DIR`,
`WHEELSHIP_BYTECODE_PYTHON`, `SOURCE_DATE_EPOCH`.

## Design

The specification and design notes are in [Design](design.md) (Japanese).
