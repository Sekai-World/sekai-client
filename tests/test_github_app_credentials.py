import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from utils import github_app_credentials as credentials


def _config(tmp_path: Path) -> Path:
    key = tmp_path / "private-key.pem"
    key.write_text("private key", encoding="ascii")
    key.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "app_id": 4325474,
                "installation_id": 147244811,
                "private_key_path": str(key),
                "repositories": ["Sekai-World/sekai-master-db-diff"],
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config


def test_get_returns_short_lived_credentials(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(
        credentials,
        "create_installation_token",
        lambda _, repository: "installation-token",
    )
    stdout = io.StringIO()

    credentials.credential_get(
        config,
        io.StringIO(
            "protocol=https\nhost=github.com\n"
            "path=Sekai-World/sekai-master-db-diff.git\n\n"
        ),
        stdout,
    )

    assert stdout.getvalue() == (
        "username=x-access-token\npassword=installation-token\n\n"
    )


def test_get_rejects_repository_outside_allowlist(tmp_path, monkeypatch):
    config = _config(tmp_path)
    token_request = Mock()
    monkeypatch.setattr(credentials, "create_installation_token", token_request)

    with pytest.raises(RuntimeError, match="not allowed"):
        credentials.credential_get(
            config,
            io.StringIO(
                "protocol=https\nhost=github.com\npath=Sekai-World/sekai-client.git\n\n"
            ),
            io.StringIO(),
        )

    token_request.assert_not_called()


def test_main_ignores_repository_outside_allowlist(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    monkeypatch.setenv("SEKAI_GITHUB_APP_CONFIG", str(config))
    monkeypatch.setattr(
        credentials.sys,
        "stdin",
        io.StringIO(
            "protocol=https\nhost=github.com\npath=Sekai-World/sekai-client.git\n\n"
        ),
    )

    assert credentials.main(["get"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_load_rejects_non_private_config(tmp_path):
    config = _config(tmp_path)
    config.chmod(0o644)

    with pytest.raises(RuntimeError, match="mode 0600"):
        credentials.load_config(config)


def test_load_rejects_non_private_key(tmp_path):
    config = _config(tmp_path)
    payload = json.loads(config.read_text())
    Path(payload["private_key_path"]).chmod(0o644)

    with pytest.raises(RuntimeError, match="mode 0600"):
        credentials.load_config(config)


def test_token_request_is_repository_scoped(tmp_path, monkeypatch):
    config = credentials.load_config(_config(tmp_path))
    monkeypatch.setattr(credentials.jwt, "encode", lambda *args, **kwargs: "app-jwt")
    response = Mock()
    response.json.return_value = {"token": "installation-token"}
    post = Mock(return_value=response)
    monkeypatch.setattr(credentials.requests, "post", post)

    assert (
        credentials.create_installation_token(
            config, "Sekai-World/sekai-master-db-diff"
        )
        == "installation-token"
    )
    response.raise_for_status.assert_called_once_with()
    assert post.call_args.kwargs["json"] == {
        "repositories": ["Sekai-World/sekai-master-db-diff"]
    }


def test_main_redacts_token_request_failure(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    monkeypatch.setenv("SEKAI_GITHUB_APP_CONFIG", str(config))
    monkeypatch.setattr(
        credentials,
        "create_installation_token",
        Mock(side_effect=RuntimeError("sensitive response")),
    )
    monkeypatch.setattr(
        credentials.sys,
        "stdin",
        io.StringIO(
            "protocol=https\nhost=github.com\n"
            "path=Sekai-World/sekai-master-db-diff.git\n\n"
        ),
    )

    assert credentials.main(["get"]) == 1
    captured = capsys.readouterr()
    assert "sensitive response" not in captured.err
