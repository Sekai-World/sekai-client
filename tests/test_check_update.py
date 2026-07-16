"""Unit tests for update source refresh selection."""

import json
from unittest.mock import Mock, call

import check_update


def test_jp_refresh_updates_split_paths_without_full_relogin(monkeypatch):
    client = Mock()
    client.request.side_effect = [True, ["master/path"], {"appVersion": "1.0"}]
    monkeypatch.setattr(check_update, "jsonrpc_client", client)
    monkeypatch.setattr(check_update, "pjsk_region", "jp")
    monkeypatch.setattr(check_update, "check_update_simple_mode", False)

    result = check_update._refresh_version_info_from_source()

    assert result == {"appVersion": "1.0"}
    assert client.request.call_args_list == [
        call("is_login"),
        call("refresh_master_split_paths"),
        call("version_info"),
    ]


def test_jp_refresh_logs_in_when_client_has_no_session(monkeypatch):
    client = Mock()
    client.request.side_effect = [False, {"user": "info"}, {"appVersion": "1.0"}]
    monkeypatch.setattr(check_update, "jsonrpc_client", client)
    monkeypatch.setattr(check_update, "pjsk_region", "jp")
    monkeypatch.setattr(check_update, "check_update_simple_mode", False)

    result = check_update._refresh_version_info_from_source()

    assert result == {"appVersion": "1.0"}
    assert client.request.call_args_list == [
        call("is_login"),
        call("login"),
        call("version_info"),
    ]


def test_merge_existing_file_data_replaces_matching_ids(tmp_path):
    file_path = tmp_path / "events.json"
    file_path.write_text(
        json.dumps(
            [
                {"id": 1, "name": "old one"},
                {"id": 2, "name": "old two"},
            ]
        )
    )

    result = check_update._merge_existing_file_data(
        str(file_path),
        [{"id": 2, "name": "new two"}, {"id": 3, "name": "new three"}],
        "id",
    )

    assert result == [
        {"id": 1, "name": "old one"},
        {"id": 2, "name": "new two"},
        {"id": 3, "name": "new three"},
    ]


def test_commit_master_diff_returns_false_on_push_failure(monkeypatch):
    """A failed commit/push must be reported as failure, not swallowed silently.

    NOTE: The current code deletes and re-clones the local repo on failure
    (see commit_master_diff / check_update.py). That data-loss behavior is a
    known issue tracked for remediation phase 4 and MUST NOT be asserted as
    correct here. This test only locks in the failure *return contract* and
    that cleanup is mocked (no real filesystem/git side effects).
    """
    repo = Mock()
    repo.is_dirty.return_value = True
    repo.remote.return_value.push.return_value.raise_if_error.side_effect = (
        RuntimeError("push rejected")
    )
    monkeypatch.setattr(check_update, "masterdb_diff_repo", repo)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(check_update.shutil, "rmtree", Mock())
    monkeypatch.setattr(check_update, "check_git_folder", Mock(return_value=repo))

    result = check_update.commit_master_diff()

    assert result is False
    repo.index.commit.assert_called_once()
    repo.remote.return_value.push.return_value.raise_if_error.assert_called_once()
