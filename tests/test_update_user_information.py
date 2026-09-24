"""Tests for the version-independent user information updater."""

import logging
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest
from git import Actor, Repo

import update_user_information as updater
from utils.git import GitOutcome, GitResult


@pytest.mark.parametrize("region", ["jp", "en", "tw", "kr"])
def test_run_once_refreshes_user_information_for_every_suite_region(
    monkeypatch, region
):
    repo = Mock()
    repo_path = updater.Path("/tmp/master-db-diff")
    commit = GitResult(GitOutcome.OK)
    push = GitResult(GitOutcome.OK)

    monkeypatch.setattr(updater, "pjsk_region", region)
    monkeypatch.setattr(updater, "masterdb_diff_folder_path", str(repo_path))
    monkeypatch.setattr(updater, "bootstrap_init_client", Mock())
    request = Mock(side_effect=[None, {"appVersion": "1", "dataVersion": "1"}])
    monkeypatch.setattr(updater.jsonrpc_client, "request", request)
    monkeypatch.setattr(updater, "_prepare_master_repo", Mock(return_value=repo))
    save_info = Mock()
    refresh_info = Mock()
    monkeypatch.setattr(updater, "save_info_from_suite_user", save_info)
    monkeypatch.setattr(updater, "refresh_information", refresh_info)
    monkeypatch.setattr(updater, "commit_diff", Mock(return_value=commit))
    monkeypatch.setattr(updater, "push_diff", Mock(return_value=push))
    monkeypatch.setattr(
        updater, "repo_file_locks", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(updater.Path, "exists", lambda self: True)

    assert updater.run_once() == "ok"
    save_info.assert_called_once_with(
        updater.jsonrpc_client, region, updater._write_master_file
    )
    if region == "en":
        refresh_info.assert_not_called()
    else:
        refresh_info.assert_called_once_with(
            updater.jsonrpc_client, updater._write_master_file
        )


def test_run_once_returns_nothing_to_do_when_information_files_are_unchanged(
    monkeypatch, tmp_path
):
    repo = Mock()
    repo_path = tmp_path / "master-db-diff"
    commit = GitResult(GitOutcome.NOTHING_TO_DO, reason="clean")

    monkeypatch.setattr(updater, "masterdb_diff_folder_path", str(repo_path))
    monkeypatch.setattr(updater, "bootstrap_init_client", Mock())
    monkeypatch.setattr(updater.jsonrpc_client, "request", Mock(return_value=None))
    monkeypatch.setattr(updater, "_prepare_master_repo", Mock(return_value=repo))
    monkeypatch.setattr(updater, "save_info_from_suite_user", Mock())
    monkeypatch.setattr(updater, "refresh_information", Mock())
    monkeypatch.setattr(updater, "commit_diff", Mock(return_value=commit))
    monkeypatch.setattr(
        updater, "repo_file_locks", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(Path, "exists", lambda self: True)

    assert updater.run_once() == "nothing_to_do"


# --------------------------------------------------------------------------- #
# Recovery of commits retained by an earlier failed push
# --------------------------------------------------------------------------- #

_UPDATER = Actor("user-information-bot", "anonymous@example.com")
_OTHER_BOT = Actor("master-db-diff-bot", "anonymous@example.com")


def _commit_files(repo: Repo, files: dict[str, str], author: Actor) -> str:
    for name, content in files.items():
        (Path(repo.working_dir) / name).write_text(content)
    repo.index.add(list(files))
    repo.index.commit("retained commit", author=author)
    return repo.head.commit.hexsha


def _repo_with_ahead_commits(
    tmp_path, ahead: list[tuple[Actor, dict[str, str]]]
) -> tuple[Repo, Repo]:
    """Return (bare remote, local repo) where the local repo is ahead by ``ahead``."""
    remote = Repo.init(str(tmp_path / "remote.git"), bare=True)
    repo = Repo.init(str(tmp_path / "master-db-diff"))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "test")
        cw.set_value("user", "email", "test@example.com")
    repo.git.checkout("-b", "main")
    _commit_files(repo, {"userInformations.json": "base"}, Actor("seed", "s@e.com"))
    repo.create_remote("origin", remote.working_dir)
    repo.git.push("origin", "main")
    for author, files in ahead:
        _commit_files(repo, files, author)
    return remote, repo


def _stub_cycle(monkeypatch, repo: Repo) -> Mock:
    """Stub the RPC side of run_once; return the save-info mock."""

    def save_info(client, region, write):
        (Path(repo.working_dir) / "userInformations.json").write_text("fresh")

    save_info_mock = Mock(side_effect=save_info)
    monkeypatch.setattr(updater, "pjsk_region", "jp")
    monkeypatch.setattr(updater, "masterdb_diff_folder_path", repo.working_dir)
    monkeypatch.setattr(updater, "bootstrap_init_client", Mock())
    monkeypatch.setattr(
        updater.jsonrpc_client,
        "request",
        Mock(
            side_effect=lambda m: {"dataVersion": "1"} if m == "version_info" else None
        ),
    )
    monkeypatch.setattr(updater, "save_info_from_suite_user", save_info_mock)
    monkeypatch.setattr(updater, "refresh_information", Mock())
    return save_info_mock


def test_run_once_pushes_retained_updater_commit_then_continues(monkeypatch, tmp_path):
    remote, repo = _repo_with_ahead_commits(
        tmp_path, [(_UPDATER, {"userInformations.json": "retained"})]
    )
    retained_sha = repo.head.commit.hexsha
    _stub_cycle(monkeypatch, repo)

    assert updater.run_once() == "ok"

    remote_head = remote.commit("main")
    assert remote_head.hexsha == repo.head.commit.hexsha
    assert remote_head.parents[0].hexsha == retained_sha
    assert remote_head.author.name == "user-information-bot"


@pytest.mark.parametrize(
    "ahead",
    [
        [(_OTHER_BOT, {"userInformations.json": "version"})],
        [(_UPDATER, {"userInformations.json": "x", "musics.json": "y"})],
        [
            (_OTHER_BOT, {"musics.json": "version"}),
            (_UPDATER, {"userInformations.json": "retained"}),
        ],
    ],
    ids=["other_author", "other_file", "mixed_history"],
)
def test_run_once_never_pushes_ahead_commits_not_owned_by_updater(
    monkeypatch, tmp_path, ahead
):
    remote, repo = _repo_with_ahead_commits(tmp_path, ahead)
    remote_sha = remote.commit("main").hexsha
    local_sha = repo.head.commit.hexsha
    save_info = _stub_cycle(monkeypatch, repo)
    push = Mock(wraps=updater.push_diff)
    monkeypatch.setattr(updater, "push_diff", push)

    with pytest.raises(RuntimeError, match="ahead_push_disabled.*not owned"):
        updater.run_once()

    push.assert_not_called()
    save_info.assert_not_called()
    assert remote.commit("main").hexsha == remote_sha
    assert repo.head.commit.hexsha == local_sha


def test_run_once_raises_when_retained_commit_push_fails(monkeypatch, tmp_path, caplog):
    remote, repo = _repo_with_ahead_commits(
        tmp_path, [(_UPDATER, {"userInformations.json": "retained"})]
    )
    remote_sha = remote.commit("main").hexsha
    retained_sha = repo.head.commit.hexsha
    hook = Path(remote.git_dir) / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    save_info = _stub_cycle(monkeypatch, repo)

    with caplog.at_level(logging.WARNING, logger="utils.git_publish"):
        with pytest.raises(
            RuntimeError, match="retained .* push failed: push_rejected"
        ):
            updater.run_once()

    save_info.assert_not_called()
    assert remote.commit("main").hexsha == remote_sha
    assert repo.head.commit.hexsha == retained_sha
    assert "pre-receive hook declined" in caplog.text
