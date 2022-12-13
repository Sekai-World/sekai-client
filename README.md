# Sekai Client

Unofficial API client for Project Sekai feat. Hatsune Miku and several useful tools written in python.

## Bootstrap

This repo use [`pipenv`][pipenv_url] to create the virtual environment for running python scripts.

To start, install `python` (version >= 3.10) and `pipenv` and run

```sh
$ pipenv --python 3.10
$ pipenv install
```

[pipenv_url]: https://pipenv.pypa.io/en/latest/

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
| `GIT_FOLDER_SEKAI_I18N` | sekai i18n git project name (default: sekai-i18n) | | ✅ | | |
| `GIT_FOLDER_SEKAI_MASTER_DB_DIFF` | sekai master db diff git project name (default: sekai-master-db-diff) | | ✅ | | |
| `REMOTE_GIT_BASE_URL` | git remote base url | | ✅ | | |
| `STRAPI_BASE_URL` | strapi base url | | ✅ | ✅ | |
| `STRAPI_TOKEN` | strapi access token | | ✅ | | |
| `SEKAI_API_KEY` | sekai api key | | | ✅ | |
| `JSONRPC_PORT` | Shared client jsonrpc port | | ✅ | ✅ | |
