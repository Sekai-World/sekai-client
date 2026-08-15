# GitHub App Authentication for Update Repositories

Production Git publishing uses the `repository-scoped-github-app` GitHub App instead
of a personal access token. The App is installed only on the six generated-data
repositories and has `Contents: write` plus `Metadata: read` permissions.

## Fixed identifiers and repository scope

- App ID: `<redacted-app-id>`
- Installation ID: `<redacted-installation-id>`
- Repositories: `sekai-i18n` and the JP, EN, TW, KR, and CN master-db diff
  repositories listed in `deployment/github-app/config.example.json`

The global credential helper checks the requested Git repository against this
allowlist and requests an installation token scoped only to that repository. It
returns no credential for another host or repository, allowing unrelated Git
credential helpers to continue normally. It does not cache tokens. Global
installation is required because regional repositories may be cloned on demand.

## Production installation

Copy the App private key to the host through an encrypted administrative
channel. Do not pass its contents through shell arguments, PM2 configuration,
Git remotes, logs, or this repository. From the deployed `main` checkout run:

```bash
uv sync --frozen
uv run python deployment/github-app/install.py \
  --private-key <private-key-path>
```

The installer copies the key to
`<protected-app-config-path>`, writes the non-secret App
configuration, replaces each existing subrepository remote with a
credential-free HTTPS URL, and installs the allowlist-aware global Git
credential helper so future regional clones also authenticate. The configuration
directory is `0700`; its key and configuration files are `0600`.

Delete the transferred source key after confirming the installed copy exists.
Never place the key under `/tmp`.

## Verification and PAT retirement

For each repository, confirm that its remote URL contains no username or token,
then perform a fetch. Validate a push using the normal update transaction rather
than creating an unrelated production commit. Review logs to ensure no helper
output or Git URL contains credentials.

Only after all six repositories authenticate through the App:

1. Revoke the old PAT.
2. Confirm it returns HTTP 401.
3. Remove any core dump or credential file containing it.
4. Run one update cycle and confirm fetch and push still succeed.

If App authentication fails, do not restore a token-bearing remote URL. Check
the App installation, selected repositories, private-key permissions, system
clock, and GitHub API reachability.
