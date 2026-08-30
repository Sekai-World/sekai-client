# PM2 deployment templates

Production currently uses one PM2 YAML file per process under `/root/pm2`.
The files in `examples/` mirror that layout while keeping credentials out of
Git.

## Rendering a template

Each sensitive value is an environment placeholder such as
`${INTERNAL_RPC_TOKEN}`. Export the required variables in a trusted shell, then
render a template with `envsubst` into `/root/pm2`:

```bash
umask 077
envsubst < deployment/pm2/examples/sharedApiClientJP.yaml.example \
  > /root/pm2/sharedApiClientJP.yaml
```

Render only the files being deployed. Validate the generated YAML without
printing secret values, then start or reload that file explicitly:

```bash
python - <<'PY'
from pathlib import Path
import yaml

for path in Path("/root/pm2").glob("*.yaml"):
    yaml.safe_load(path.read_text())
    print(f"valid: {path.name}")
PY

pm2 startOrReload /root/pm2/sharedApiClientJP.yaml --update-env
```

Do not commit rendered YAML files. Keep `/root/pm2/*.yaml` mode `0600` because
they contain expanded credentials.

Event tracker templates persist their SQLite delivery outboxes under
`/root/sekai-client/.runtime`. Create that directory with mode `0700` before
activation, keep each database at mode `0600`, and include the database plus
its WAL files in backup and disk-usage monitoring. Do not place an outbox under
`/tmp`; a process restart must not discard pending ranking snapshots. The
default terminal-record retention is 24 hours, the per-run drain budget is 30
seconds, and the individual delivery timeout is 15 seconds; tune these only
with measured scheduler and upstream latency evidence.

## Security requirements

- Use the same non-empty `INTERNAL_RPC_TOKEN` for all formal shared clients,
  check-update workers, event trackers, and the public API.
- The standalone `checkUpdate-cn` process does not use internal RPC and must not
  receive `INTERNAL_RPC_TOKEN`.
- `updateUserInformation-{jp,en,tw,kr}` refreshes user information every 30
  minutes without checking for a new game version. It owns only
  `userHomeBanners.json` and `userInformations.json`; keep
  `ENABLE_SEKAI_UPDATE_USER_INFO` disabled on all `checkUpdate-*` processes.
  Each updater must use its regional master repository: JP uses
  `sekai-master-db-diff`, EN uses `sekai-master-db-en-diff`, TW uses
  `sekai-master-db-tc-diff`, and KR uses `sekai-master-db-kr-diff`.
- `API_TOKEN` belongs only to the public API/Dashboard process.
- Do not set `ALLOW_INSECURE_INTERNAL_RPC` or `ENABLE_UNSAFE_PJSK_RPC` in
  production.
- Never embed a GitHub personal access token in `REMOTE_GIT_BASE_URL`. Production
  uses the repository-scoped GitHub App credential helper documented in
  [GitHub App Authentication](../../docs/github-app-git-authentication.md).

## Why YAML is retained

The per-process YAML layout matches the current operational deployment and
allows individual services to be updated independently. A single
`ecosystem.config.js` would reduce duplication, but it would also be a deployment
migration rather than a documentation-only change. Migrate only after all
per-region settings are modeled and the generated process definitions are
compared with the running PM2 environment.
