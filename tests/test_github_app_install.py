import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock


def _installer_module():
    path = Path(__file__).parents[1] / "deployment" / "github-app" / "install.py"
    spec = importlib.util.spec_from_file_location("github_app_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_configures_clean_remotes_and_global_helper(tmp_path, monkeypatch):
    installer = _installer_module()
    project = tmp_path / "sekai-client"
    (project / "utils").mkdir(parents=True)
    (project / "utils" / "github_app_credentials.py").touch()
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    for name in installer.REPOSITORIES:
        (project / name).mkdir()
    source_key = tmp_path / "source.pem"
    source_key.write_text("private key", encoding="ascii")
    config_directory = tmp_path / "config"
    git_calls = []
    monkeypatch.setattr(
        installer,
        "_run_git",
        lambda repository, *arguments: git_calls.append((repository, arguments)),
    )
    monkeypatch.setattr(installer.subprocess, "run", Mock())

    installer.install(project, config_directory, source_key, 4325474, 147244811)

    config = json.loads((config_directory / "config.json").read_text())
    assert config["app_id"] == 4325474
    assert config["installation_id"] == 147244811
    assert len(config["repositories"]) == 6
    assert (config_directory / "private-key.pem").stat().st_mode & 0o777 == 0o600
    remote_calls = [
        call for call in git_calls if call[1][:3] == ("remote", "set-url", "origin")
    ]
    assert len(remote_calls) == 6
    assert all("@" not in call[1][3] for call in remote_calls)
    assert all(call[1][3].startswith("https://github.com/") for call in remote_calls)
    global_commands = [call.args[0] for call in installer.subprocess.run.call_args_list]
    assert any("credential.helper" in command for command in global_commands)
    assert any(
        "credential.https://github.com.useHttpPath" in command
        for command in global_commands
    )


def test_install_allows_repositories_that_are_created_later(tmp_path, monkeypatch):
    installer = _installer_module()
    project = tmp_path / "sekai-client"
    (project / "utils").mkdir(parents=True)
    (project / "utils" / "github_app_credentials.py").touch()
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    (project / "sekai-master-db-diff").mkdir()
    source_key = tmp_path / "source.pem"
    source_key.write_text("private key", encoding="ascii")
    git_calls = []
    monkeypatch.setattr(
        installer,
        "_run_git",
        lambda repository, *arguments: git_calls.append((repository, arguments)),
    )
    monkeypatch.setattr(installer.subprocess, "run", Mock())

    installer.install(project, tmp_path / "config", source_key, 4325474, 147244811)

    remote_calls = [
        call for call in git_calls if call[1][:3] == ("remote", "set-url", "origin")
    ]
    assert len(remote_calls) == 1


def test_install_rejects_non_private_existing_config_directory(tmp_path, monkeypatch):
    installer = _installer_module()
    project = tmp_path / "sekai-client"
    (project / "utils").mkdir(parents=True)
    (project / "utils" / "github_app_credentials.py").touch()
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    for name in installer.REPOSITORIES:
        (project / name).mkdir()
    source_key = tmp_path / "source.pem"
    source_key.touch()
    config_directory = tmp_path / "config"
    config_directory.mkdir(mode=0o755)
    monkeypatch.setattr(installer, "_run_git", Mock())
    monkeypatch.setattr(installer.subprocess, "run", Mock())

    try:
        installer.install(project, config_directory, source_key, 4325474, 147244811)
    except RuntimeError as error:
        assert "private and owner-controlled" in str(error)
    else:
        raise AssertionError("non-private directory was accepted")
