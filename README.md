# wheelship

Build relocatable Python application bundles for Linux, Windows and AWS Lambda.
Design: [docs/design.md](docs/design.md).

## Layout

- `packages/wheelship/` — the Python tool (uv workspace member)
- `crates/wheelship-launcher/` — the native launcher (cargo workspace member, Phase 1)
- `tests/` — unit and integration tests; `tests/fixtures/apps/` are standalone sample projects

## Development

```sh
uv sync                      # create .venv with wheelship (editable) + dev tools
uv run pytest                # WHEELSHIP_OFFLINE=1 skips tests that need the network
uv run ruff check . && uv run ruff format --check . && uv run mypy
cargo build                  # launcher (needs a Rust toolchain)
```

## Usage

```sh
cd your-project
uv export --format pylock.toml --no-dev -o pylock.toml   # lock dependencies (PEP 751)
wheelship targets                                            # show tags / markers per target
wheelship build                                              # all configured targets -> dist/
wheelship build -t linux -f archive                          # one target, one format
wheelship build --platform lambda-arm64 --python 3.13 -f lambda-zip   # ad-hoc target
```

Formats implemented so far:

| format | interpreter | output |
|---|---|---|
| `dir` / `archive` | bundled or system | directory / `.tar.gz` (Linux) or `.zip` (Windows) |
| `exe` | bundled or system | single file (`.exe` / `.bin`), extracted once into a cache |
| `pyz` | system | PEP 441 zipapp |
| `lambda-zip` / `lambda-layer` | Lambda managed runtime | deployment package / layer |
| `lambda-runtime` | bundled (`provided.al2023`) | custom runtime: `bootstrap` + python-build-standalone + awslambdaric |

Every target also gets a CycloneDX SBOM (`<name>-<version>-<target>.cdx.json`).

`interpreter = "bundled"` ships python-build-standalone; `"system"` uses CPython of
the target's version found on `PATH` (or `WHEELSHIP_PYTHON`).
The python-build-standalone release is pinned in `wheelship.lock` (commit it).
Examples: `tests/fixtures/apps/*/pyproject.toml`.

Runtime environment variables: `WHEELSHIP_DEBUG=1` (show what is executed),
`WHEELSHIP_BOOT=python` (run the interpreter directly), `WHEELSHIP_PYTHON`
(interpreter for system bundles), `WHEELSHIP_CACHE_DIR` (where `exe`/`pyz` extract),
`WHEELSHIP_EMBED=1` (experimental, Windows bundled: run Python inside the launcher process).
Build-time: `WHEELSHIP_CACHE_DIR`, `WHEELSHIP_PBS_MIRROR`, `WHEELSHIP_LAUNCHER_DIR`,
`WHEELSHIP_BYTECODE_PYTHON`, `SOURCE_DATE_EPOCH`.

During development the launcher is taken from `target/` after `cargo build --release`
(host only); release wheels carry prebuilt launchers for every target (built in CI).
