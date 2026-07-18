"""Unit tests for update source refresh selection."""

import json
from unittest.mock import Mock, call

import pytest
import requests

import check_update
from utils.git import GitOutcome


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


def test_commit_master_diff_returns_pending_on_push_failure(monkeypatch):
    """A push failure must be reported as PENDING_PUSH while keeping the commit.

    The repository must NOT be deleted, recloned, reset, or force-pushed. The
    historical boolean contract (``__bool__``) maps an ``OK`` result to truthy
    and a ``PENDING_PUSH`` result to falsy, so production
    ``if commit_master_diff():`` paths still treat push failure as failure.
    """
    repo = Mock()
    repo.is_dirty.return_value = True
    # push_current_head returns a GitResult; emulate a PENDING_PUSH push.
    pending = check_update.GitResult(
        outcome=GitOutcome.PENDING_PUSH, reason="push_rejected", local_sha="abc"
    )
    monkeypatch.setattr(check_update, "push_current_head", Mock(return_value=pending))
    monkeypatch.setattr(check_update, "masterdb_diff_repo", repo)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    # Ensure no destructive cleanup helpers are reachable from this path.
    monkeypatch.setattr(check_update, "check_git_folder", Mock())

    result = check_update.commit_master_diff()

    assert result.outcome is GitOutcome.PENDING_PUSH
    assert bool(result) is False
    repo.index.commit.assert_called_once()
    check_update.push_current_head.assert_called_once()
    # The repo must not be recloned/destroyed on push failure.
    check_update.check_git_folder.assert_not_called()


def test_commit_master_diff_returns_failed_on_commit_error(monkeypatch):
    """A commit (stage/commit) failure is distinct from a push failure."""
    repo = Mock()
    repo.is_dirty.return_value = True
    repo.index.commit.side_effect = RuntimeError("nothing staged")

    monkeypatch.setattr(check_update, "masterdb_diff_repo", repo)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(check_update, "push_current_head", Mock())

    result = check_update.commit_master_diff()

    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "commit_failed"
    assert bool(result) is False
    # A failed commit must never attempt to push.
    check_update.push_current_head.assert_not_called()


def test_commit_master_diff_returns_nothing_to_do_when_clean(monkeypatch):
    repo = Mock()
    repo.is_dirty.return_value = False
    monkeypatch.setattr(check_update, "masterdb_diff_repo", repo)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(check_update, "push_current_head", Mock())

    result = check_update.commit_master_diff()

    assert result.outcome is GitOutcome.NOTHING_TO_DO
    repo.index.commit.assert_not_called()
    check_update.push_current_head.assert_not_called()


def test_commit_master_diff_returns_failed_when_repo_missing(monkeypatch):
    monkeypatch.setattr(check_update, "masterdb_diff_repo", None)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(check_update, "push_current_head", Mock())

    result = check_update.commit_master_diff()

    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "repo_missing"
    check_update.push_current_head.assert_not_called()


def test_commit_master_diff_returns_failed_when_version_info_missing(monkeypatch):
    repo = Mock()
    repo.is_dirty.return_value = True
    monkeypatch.setattr(check_update, "masterdb_diff_repo", repo)
    monkeypatch.setattr(check_update, "version_info", None)
    monkeypatch.setattr(check_update, "push_current_head", Mock())

    result = check_update.commit_master_diff()

    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "version_info_missing"
    repo.index.commit.assert_not_called()
    check_update.push_current_head.assert_not_called()


def test_commit_i18n_files_parity_with_master(monkeypatch):
    """i18n wrapper is symmetric with master: same structured contract."""
    repo = Mock()
    repo.is_dirty.return_value = True
    pending = check_update.GitResult(
        outcome=GitOutcome.PENDING_PUSH, reason="push_rejected", local_sha="abc"
    )
    monkeypatch.setattr(check_update, "push_current_head", Mock(return_value=pending))
    monkeypatch.setattr(check_update, "i18n_diff_repo", repo)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(check_update, "check_git_folder", Mock())

    result = check_update.commit_i18n_files()

    assert result.outcome is GitOutcome.PENDING_PUSH
    assert bool(result) is False
    repo.index.commit.assert_called_once()
    check_update.push_current_head.assert_called_once()
    check_update.check_git_folder.assert_not_called()


def test_commit_i18n_files_failed_when_repo_missing(monkeypatch):
    monkeypatch.setattr(check_update, "i18n_diff_repo", None)
    monkeypatch.setattr(
        check_update, "version_info", {"dataVersion": "1", "assetVersion": "1"}
    )
    monkeypatch.setattr(check_update, "push_current_head", Mock())

    result = check_update.commit_i18n_files()

    assert result.outcome is GitOutcome.FAILED
    assert result.reason == "repo_missing"


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


# --------------------------------------------------------------------------- #
# Deadline unit tests (cooperative update-cycle deadline)
# --------------------------------------------------------------------------- #


def test_deadline_disabled_never_expires():
    """A ``None`` deadline is disabled and never raises."""
    d = check_update.Deadline(None)
    assert d.enabled is False
    assert d.expired() is False
    d.check()  # must not raise


def test_deadline_valid_finite_nonnegative():
    """Finite non-negative seconds build an enabled, not-yet-expired deadline."""
    d = check_update.Deadline(3600)
    assert d.enabled is True
    assert d.expired() is False
    d.check()  # must not raise


def test_deadline_zero_is_expired_immediately():
    """A zero-second deadline is already expired at construction."""
    d = check_update.Deadline(0)
    assert d.enabled is True
    assert d.expired() is True
    with pytest.raises(check_update.CycleDeadlineExceeded):
        d.check()


def test_deadline_rejects_negative():
    with pytest.raises(ValueError):
        check_update.Deadline(-1)


def test_deadline_rejects_infinite():
    with pytest.raises(ValueError):
        check_update.Deadline(float("inf"))


def test_deadline_rejects_non_number():
    with pytest.raises(ValueError):
        check_update.Deadline("3600")  # type: ignore[arg-type]


def test_deadline_expires_after_interval(monkeypatch):
    """A deadline expires once the monotonic budget elapses."""
    fake = {"t": 1000.0}
    monkeypatch.setattr(check_update, "_monotonic", lambda: fake["t"])
    d = check_update.Deadline(10)
    assert d.expired() is False
    fake["t"] = 1010.0
    assert d.expired() is True
    with pytest.raises(check_update.CycleDeadlineExceeded):
        d.check()
