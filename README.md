# pybox

Build relocatable Python application bundles for Linux, Windows and AWS Lambda.
Design: [docs/design.md](docs/design.md).

## Layout

- `packages/pybox/` — the Python tool (uv workspace member)
- `crates/pybox-launcher/` — the native launcher (cargo workspace member, Phase 1)
- `tests/` — unit and integration tests; `tests/fixtures/apps/` are standalone sample projects

## Development

```sh
uv sync                      # create .venv with pybox (editable) + dev tools
uv run pytest                # PYBOX_OFFLINE=1 skips tests that need the network
uv run ruff check . && uv run ruff format --check . && uv run mypy
cargo build                  # launcher (needs a Rust toolchain)
```

## Usage

```sh
cd your-project
uv export --format pylock.toml --no-dev -o pylock.toml   # lock dependencies (PEP 751)
pybox targets                                            # show tags / markers per target
pybox build                                              # all configured targets -> dist/
pybox build -t linux -f archive                          # one target, one format
pybox build --platform lambda-arm64 --python 3.13 -f lambda-zip   # ad-hoc target
```

Formats implemented so far: `lambda-zip`, `lambda-layer` (managed runtime) and
`dir`, `archive` (bundled python-build-standalone interpreter + launcher).
The python-build-standalone release is pinned in `pybox.lock` (commit it).
Examples: `tests/fixtures/apps/*/pyproject.toml`.

Launcher environment variables: `PYBOX_DEBUG=1` (show what is executed),
`PYBOX_BOOT=python` (run the bundled interpreter directly).
Build-time: `PYBOX_CACHE_DIR`, `PYBOX_PBS_MIRROR`, `PYBOX_LAUNCHER_DIR`,
`PYBOX_BYTECODE_PYTHON`, `SOURCE_DATE_EPOCH`.

During development the launcher is taken from `target/` after `cargo build --release`
(host only); release wheels carry prebuilt launchers for every target (built in CI).
