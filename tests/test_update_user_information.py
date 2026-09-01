"""Tests for the version-independent user information updater."""

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

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
