# Development Tooling and Commands

## Tooling Policy
This project is configured to use Ruff as the single formatting/linting tool.

Configured in [pyproject.toml](pyproject.toml):

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

1. Add CI workflow to enforce Ruff, mypy, and pytest checks.
2. Expand integration tests for JSON-RPC endpoints.
3. Add deployment and troubleshooting docs under [docs](docs).
