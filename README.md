# wheelship

Build relocatable Python application bundles for Linux, Windows and AWS Lambda.
Documentation: https://bayashi-cl.github.io/wheelship/ — start with the
[Quick Start](https://bayashi-cl.github.io/wheelship/quickstart/).
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
uv run --group docs zensical serve   # docs site at http://localhost:8000
```

## Usage

```sh
cd your-project
uv lock                      # uv projects: wheelship exports uv.lock to PEP 751 at build time
wheelship build              # all configured targets -> dist/
```

See the [documentation](https://bayashi-cl.github.io/wheelship/) for formats, configuration
and environment variables.

During development the launcher is taken from `target/` after `cargo build --release`
(host only); release wheels carry prebuilt launchers for every target (built in CI).
