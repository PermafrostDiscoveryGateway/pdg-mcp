# Contributing

## Setup and tests

```bash
uv sync
uv run pytest                 # live tests (calling arcticdata.io) are skipped by default
uv run pytest -m live         # run the live tests
```

## Linting and formatting

CI runs [ruff](https://docs.astral.sh/ruff/) (config in `ruff.toml`) and fails if either check does:

```bash
uv run ruff check .           # add --fix to apply safe fixes
uv run ruff format .          # CI runs `ruff format --check .`
```

To run these automatically before each commit, install the optional hook:

```bash
uvx pre-commit install
```
