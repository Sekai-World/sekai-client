# Sekai Client

Unofficial API client for Project Sekai feat. Hatsune Miku and several useful tools written in python.

## Bootstrap

This repo uses [`uv`][uv_url] to create the virtual environment and manage dependencies for running python scripts. The project targets Python 3.12.

To start, install `uv` and run

```sh
$ uv sync
```

[uv_url]: https://docs.astral.sh/uv/

## Tools

| Tool | Description |
| --- | --- |
| `api_client` | PJSK headless api client |
| `api_public_server` | A flask app exposing some PJSK api to public |
| `check_update` | Check PJSK game data and assets update |
| `event_tracker` | Track PJSK event rankings and cutoffs |
| `shared_client` | A shared PJSK api client for all other tools |

## Configure environment variables

Some files need to have specific environment variables, listed below

| Variable name | Description | shared_client | check_update | event_tracker | api_public_server |
| --- | --- | :---: | :---: | :---: | :---: |
| `APP_VER` | PJSK app version | ✅ | | | |
| `AES_KEY` | PJSK aes key | ✅ | | | |
| `AES_IV` | PJSK aes iv | ✅ | | | |
| `SEKAI_TW_DEVICE_ID` | Device id for tw server | ✅ (pjsk_region=tw) | | | |
| `SEKAI_TW_ACCESS_TOKEN` | Access token for tw server | ✅ (pjsk_region=tw) | | | |
| `SEKAI_TW_SDK_OPEN_ID` | SDK open id for tw server | ✅ (pjsk_region=tw) | | | |
| `SEKAI_KR_DEVICE_ID` | Device id for kr server | ✅ (pjsk_region=kr) | | | |
| `SEKAI_KR_ACCESS_TOKEN` | Access token for kr server | ✅ (pjsk_region=kr) | | | |
| `SEKAI_KR_SDK_OPEN_ID` | SDK open id for kr server | ✅ (pjsk_region=kr) | | | |
| `SEKAI_REGION` | PJSK game region | ✅ | ✅ | ✅ | |
| `ENABLE_SEKAI_UPDATE_MASTER` | whether to update sekai master db | | ✅ | | |
| `ENABLE_SEKAI_UPDATE_USER_INFO` | whether to update sekai user info | | ✅ | | |
| `ENABLE_SEKAI_UPDATE_I18N` | whether to update sekai i18n files | | ✅ | | |
| `CHECK_UPDATE_SIMPLE_MODE` | use versions.json without shared client for Nuverse servers | | ✅ | | |
| `CHECK_UPDATE_VERSIONS_URL` | versions.json URL for simple check_update mode | | ✅ | | |
| `GIT_FOLDER_SEKAI_I18N` | sekai i18n git project name (default: sekai-i18n) | | ✅ | | |
| `GIT_FOLDER_SEKAI_MASTER_DB_DIFF` | sekai master db diff git project name (default: sekai-master-db-diff) | | ✅ | | |
| `REMOTE_GIT_BASE_URL` | git remote base url | | ✅ | | |
| `STRAPI_BASE_URL` | strapi base url | | ✅ | ✅ | |
| `STRAPI_TOKEN` | strapi access token | | ✅ | | |
| `SEKAI_API_KEY` | sekai api key | | | ✅ | |
| `JSONRPC_PORT` | Shared client jsonrpc port | | ✅ | ✅ | |
| `INTERNAL_RPC_TOKEN` | Auth token for internal JSON-RPC between shared_client / check_update / event_tracker / api_public_server (required, loopback only) | ✅ | ✅ | ✅ | ✅ |
| `ALLOW_INSECURE_INTERNAL_RPC` | Dev-only: allow unauthenticated RPC from loopback (127.0.0.1/::1). Non-loopback is always rejected. Off by default (fail-closed). | ✅ | ✅ | ✅ | ✅ |
| `ENABLE_UNSAFE_PJSK_RPC` | Dev-only: expose the generic `call_pjsk_api` RPC (disabled by default; use scoped `fetch_master_split` instead). | ✅ | | | |

## Security: internal RPC auth

The four processes communicate over loopback via an internal JSON-RPC protocol.
Every call must carry `INTERNAL_RPC_TOKEN` (set identically in the environment
of all processes). It is read dynamically from `INTERNAL_RPC_TOKEN` and sent as
the `x-internal-rpc-token` header by the client, and checked with constant-time
comparison by the server.

- If `INTERNAL_RPC_TOKEN` is not set, requests fail closed: `500` unless
  `ALLOW_INSECURE_INTERNAL_RPC=true` **and** the caller is on loopback
  (`127.0.0.1` / `::1`).
- A wrong/missing token is rejected with `401`.
- A non-loopback caller is always rejected, even with the insecure bypass enabled.
- Token rotation requires coordinating all related processes so callers and
  shared clients use the same environment. With PM2, reload/restart from the
  ecosystem file and update the process environment for all formal services;
  do not assume changing the shell environment updates already-running Python
  processes.

The standalone `checkUpdate-cn` process does not use internal RPC and does not
require `INTERNAL_RPC_TOKEN`.

Credentials (tokens, cookies, signatures, device IDs) are redacted from all
logs via a logging filter installed by `configure_logging()`.

## Configure environment variables

See the table above. The `shared_client` credential files
(`sharedAccount.{region}.yaml`) are written atomically with `0600` permissions;
existing files are chmod'd to `0600` on read on POSIX. On Windows this is
best-effort and does not replace a real secret store or ACL policy.
