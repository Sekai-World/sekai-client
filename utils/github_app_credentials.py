"""Git credential helper backed by short-lived GitHub App installation tokens."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_IMODE
from typing import TextIO

import jwt
import requests

DEFAULT_CONFIG = Path("/root/.config/sekai-github-app/config.json")
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class AppConfig:
    app_id: int
    installation_id: int
    private_key_path: Path
    repositories: frozenset[str]


def _require_private_file(path: Path, label: str) -> None:
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"{label} must be owner-controlled with mode 0600")
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label} must be a regular file")


def load_config(path: Path) -> AppConfig:
    _require_private_file(path, "GitHub App config")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        config = AppConfig(
            app_id=int(payload["app_id"]),
            installation_id=int(payload["installation_id"]),
            private_key_path=Path(payload["private_key_path"]),
            repositories=frozenset(str(item) for item in payload["repositories"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("GitHub App config is invalid") from error
    if config.app_id <= 0 or config.installation_id <= 0 or not config.repositories:
        raise RuntimeError("GitHub App config is invalid")
    _require_private_file(config.private_key_path, "GitHub App private key")
    return config


def parse_credential_request(stream: TextIO) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in stream:
        line = raw_line.rstrip("\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return fields


def requested_repository(fields: dict[str, str]) -> str:
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        raise RuntimeError("credential request is not for github.com HTTPS")
    repository = fields.get("path", "").removesuffix(".git").strip("/")
    if repository.count("/") != 1:
        raise RuntimeError("credential request has no repository path")
    return repository


def create_installation_token(config: AppConfig, repository: str) -> str:
    now = datetime.now(UTC)
    private_key = config.private_key_path.read_text(encoding="ascii")
    app_jwt = jwt.encode(
        {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": str(config.app_id),
        },
        private_key,
        algorithm="RS256",
    )
    try:
        response = requests.post(
            f"{GITHUB_API}/app/installations/{config.installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"repositories": [repository]},
            timeout=10,
        )
        response.raise_for_status()
        token = response.json()["token"]
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("GitHub App token request failed") from error
    if not isinstance(token, str) or not token:
        raise RuntimeError("GitHub App token response is invalid")
    return token


def credential_get(config_path: Path, stdin: TextIO, stdout: TextIO) -> None:
    config = load_config(config_path)
    repository = requested_repository(parse_credential_request(stdin))
    if repository not in config.repositories:
        raise RuntimeError("repository is not allowed by GitHub App configuration")
    token = create_installation_token(config, repository)
    stdout.write("username=x-access-token\n")
    stdout.write(f"password={token}\n\n")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    operation = arguments[0] if arguments else ""
    if operation in {"store", "erase"}:
        return 0
    if operation != "get":
        return 1
    config_path = Path(os.environ.get("SEKAI_GITHUB_APP_CONFIG", str(DEFAULT_CONFIG)))
    try:
        credential_get(config_path, sys.stdin, sys.stdout)
    except Exception:
        print("GitHub App credential helper failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
