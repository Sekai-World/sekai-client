"""Tests for the version-independent user information updater."""

from contextlib import nullcontext
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

    monkeypatch.setattr(updater.update, "pjsk_region", region)
    monkeypatch.setattr(updater.update, "masterdb_diff_folder_path", str(repo_path))
    monkeypatch.setattr(updater.update, "_bootstrap_init_client", Mock())
    request = Mock(side_effect=[None, {"appVersion": "1", "dataVersion": "1"}])
    monkeypatch.setattr(updater.update.jsonrpc_client, "request", request)
    monkeypatch.setattr(updater, "_prepare_master_repo", Mock(return_value=repo))
    save_info = Mock()
    refresh_info = Mock()
    monkeypatch.setattr(updater.update, "save_info_from_suite_user", save_info)
    monkeypatch.setattr(updater.update, "refresh_information", refresh_info)
    monkeypatch.setattr(updater.update, "_commit_diff", Mock(return_value=commit))
    monkeypatch.setattr(updater.update, "_push_diff", Mock(return_value=push))
    monkeypatch.setattr(
        updater, "repo_file_locks", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(updater.Path, "exists", lambda self: True)

    assert updater.run_once() == "ok"
    save_info.assert_called_once_with()
    if region == "en":
        refresh_info.assert_not_called()
    else:
        refresh_info.assert_called_once_with()
