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

## Usage (Phase 0: AWS Lambda)

```sh
cd your-project
uv export --format pylock.toml --no-dev -o pylock.toml
uv run --project /path/to/this/repo pybox targets        # show tags / markers per target
uv run --project /path/to/this/repo pybox build          # -> dist/*.zip
pybox build --platform lambda-arm64 --python 3.13 -f lambda-zip   # ad-hoc target
```

See `tests/fixtures/apps/lambda_app/pyproject.toml` for a `[tool.pybox]` example.
