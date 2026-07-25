# Development Tooling and Commands

## Tooling Policy
This project is configured to use Ruff as the single formatting/linting tool.

The `dev` extra includes broader developer tooling as well
(`pytest`, `pytest-cov`, `pytest-mock`, `mypy`, and `ruff`).

Configured in [pyproject.toml](../pyproject.toml):

- Ruff formatter: `ruff format`
- Ruff linter: `ruff check`
- Import sorting: Ruff `I` rules

## Environment Setup

```bash
uv sync --extra dev
```

## Common Local Commands

### Run tests

```bash
uv run --extra dev pytest tests/
uv run --extra dev pytest tests/ --cov
```

### Run type check

```bash
uv run --extra dev mypy api_client.py shared_client.py utils/ config.py
```

### Run Ruff formatter

```bash
uv run --extra dev ruff format .
uv run --extra dev ruff format --check .
```

### Run Ruff lint

```bash
uv run --extra dev ruff check .
uv run --extra dev ruff check . --fix
```

## Recommended Pre-PR Checks

```bash
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev pytest tests/
```

## Next Technical Steps

1. Confirm `transform-python` branch protection requires the existing CI workflow's Ruff, mypy, and pytest checks, and keep the workflow maintained as project checks evolve.
2. Expand integration tests for JSON-RPC endpoints and upstream API response validation.
3. Add and maintain deployment and troubleshooting docs under [docs](.).
