# Contributing

Thanks for helping improve sekai-client.

## Setup

Use [uv](https://docs.astral.sh/uv/) for the Python environment and tooling:

```bash
uv sync
```

## Checks

Run tests:

```bash
uv run pytest
```

Lint / style:

```bash
uv run ruff check .
```

## Pull requests

In the PR description, please cover:

- **Scope** — what changed and why
- **Behavior** — user-visible or operational impact
- **Tests** — what you ran or added, and results

Keep PRs focused and easy to review.

## Do not commit

Never commit:

- Credentials, API tokens, private keys, or `.env` files
- Game account data
- Generated event/profile data (for example `event-*.json`, `profile-*.json`)

Use deployment examples under `deployment/pm2/examples/` (`*.example.yaml`) as templates; keep real configs local and untracked.
