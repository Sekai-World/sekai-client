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
