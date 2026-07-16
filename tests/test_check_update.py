"""Unit tests for update source refresh selection."""

import json
from unittest.mock import Mock, call

import requests

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


def test_post_strapi_ids_uses_authorization_header_not_query(monkeypatch):
    import check_update as cu

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        resp = Mock()
        resp.raise_for_status.return_value = None
        return resp

    captured: dict = {}
    monkeypatch.setattr(cu, "strapi_base_url", "http://strapi:3000")
    monkeypatch.setattr(cu, "strapi_token", "SECRET")
    monkeypatch.setattr(cu.requests, "post", fake_post)

    cu._post_strapi_ids("cards/fromDB", [1, 2])

    # No token in the URL query string.
    assert "SECRET" not in captured["url"]
    assert "token=" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer SECRET"
    assert captured["headers"]["X-Strapi-Token"] == "SECRET"


def test_post_strapi_ids_logs_and_continues_on_http_error(monkeypatch):
    import check_update as cu

    def fake_post(url, **kwargs):
        resp = Mock()
        resp.raise_for_status.side_effect = requests.HTTPError("500")
        captured["response"] = resp
        return resp

    captured: dict = {}
    monkeypatch.setattr(cu, "strapi_base_url", "http://strapi:3000")
    monkeypatch.setattr(cu, "strapi_token", "SECRET")
    monkeypatch.setattr(cu.requests, "post", fake_post)

    cu._post_strapi_ids("cards/fromDB", [1])

    captured["response"].raise_for_status.assert_called_once()


def test_get_splitted_master_data_uses_fetch_master_split(monkeypatch):
    import check_update as cu

    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, params=None):
            self.calls.append((method, params))
            if method == "master_split_paths":
                return ["suite/master/a", "suite/master/b"]
            return {"data": method}

    fake = FakeClient()
    monkeypatch.setattr(cu, "jsonrpc_client", fake)
    monkeypatch.setattr(cu, "pjsk_region", "jp")

    cu.get_splitted_master_data()

    assert ("master_split_paths", None) in fake.calls
    assert all(
        method == "fetch_master_split"
        for method, _ in fake.calls
        if method != "master_split_paths"
    )
