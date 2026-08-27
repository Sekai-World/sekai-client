"""Unit tests for update source refresh selection."""

import json
from unittest.mock import Mock, call

import pytest

import check_update
from utils.git import GitOutcome


def test_jp_refresh_updates_split_paths_without_full_relogin(monkeypatch):
    client = Mock()
    client.request.side_effect = [
        True,
        ["master/path"],
        {
            "appVersion": "1.0",
            "dataVersion": "1.0",
            "assetVersion": "1.0",
        },
    ]
    monkeypatch.setattr(check_update, "jsonrpc_client", client)
    monkeypatch.setattr(check_update, "pjsk_region", "jp")
    monkeypatch.setattr(check_update, "check_update_simple_mode", False)

    result = check_update._refresh_version_info_from_source()

    assert result == {
        "appVersion": "1.0",
        "dataVersion": "1.0",
        "assetVersion": "1.0",
    }
    assert client.request.call_args_list == [
        call("is_login"),
        call("refresh_master_split_paths"),
        call("version_info"),
    ]


def test_jp_refresh_logs_in_when_client_has_no_session(monkeypatch):
    client = Mock()
    client.request.side_effect = [
        False,
        {"user": "info"},
        {
            "appVersion": "1.0",
            "dataVersion": "1.0",
            "assetVersion": "1.0",
        },
    ]
    monkeypatch.setattr(check_update, "jsonrpc_client", client)
    monkeypatch.setattr(check_update, "pjsk_region", "jp")
    monkeypatch.setattr(check_update, "check_update_simple_mode", False)

    result = check_update._refresh_version_info_from_source()

    assert result == {
        "appVersion": "1.0",
        "dataVersion": "1.0",
        "assetVersion": "1.0",
    }
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


def test_save_info_from_suite_user_refreshes_en_information_once(monkeypatch):
    client = Mock()
    client.request.side_effect = [
        {"userHomeBanners": [], "userInformations": []},
        {"informations": [{"id": 1}]},
    ]
    writes = []
    monkeypatch.setattr(check_update, "jsonrpc_client", client)
    monkeypatch.setattr(check_update, "pjsk_region", "en")
    monkeypatch.setattr(
        check_update, "_write_master_file", lambda *args: writes.append(args)
    )

    check_update.save_info_from_suite_user()

    assert client.request.call_args_list == [
        call("login_user_info"),
        call("fetch_information"),
    ]
    assert writes == [
        ("userHomeBanners.json", []),
        ("userInformations.json", [{"id": 1}]),
    ]


def test_save_info_from_suite_user_keeps_non_en_user_information_write(monkeypatch):
    client = Mock()
    client.request.return_value = {
        "userHomeBanners": [],
        "userInformations": [{"id": 1}],
    }
    writes = []
    monkeypatch.setattr(check_update, "jsonrpc_client", client)
    monkeypatch.setattr(check_update, "pjsk_region", "jp")
    monkeypatch.setattr(
        check_update, "_write_master_file", lambda *args: writes.append(args)
    )

    check_update.save_info_from_suite_user()

    assert writes == [
        ("userHomeBanners.json", []),
        ("userInformations.json", [{"id": 1}]),
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


def test_post_strapi_ids_does_not_post_during_generation(tmp_path, monkeypatch):
    import check_update as cu

    posts: list[object] = []

    def fake_post(*args, **kwargs):
        posts.append((args, kwargs))
        raise AssertionError("generation must not POST to Strapi")

    outbox_path = tmp_path / "outbox.json"
    monkeypatch.setattr(cu, "_STRAPI_OUTBOX_PATH", str(outbox_path))
    monkeypatch.setattr(cu, "strapi_base_url", "http://strapi:3000")
    monkeypatch.setattr(cu, "strapi_token", "SECRET")
    monkeypatch.setattr(cu.requests, "post", fake_post)
    monkeypatch.setattr(cu, "_ACTIVE_TXN_ID", "txn-generation")

    cu._post_strapi_ids("cards/fromDB", [501, 502])

    assert posts == []
    payload = json.loads(outbox_path.read_text())
    assert len(payload["records"]) == 1
    record = next(iter(payload["records"].values()))
    assert record["endpoint"] == "cards/fromDB"
    assert record["ids"] == [501, 502]
    assert record["ready"] is False
    assert record["transaction_id"] == "txn-generation"


def test_strapi_outbox_drain_success_returns_and_uses_headers(tmp_path):
    from utils.strapi_outbox import StrapiOutbox

    outbox = StrapiOutbox(str(tmp_path / "outbox.json"))
    outbox.enqueue("cards/fromDB", [502, 501], transaction_id="txn-ready")
    assert outbox.mark_transaction_ready("txn-ready") == 1

    posts: list[dict] = []

    def fake_post(url, **kwargs):
        posts.append(
            {
                "url": url,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers"),
                "timeout": kwargs.get("timeout"),
            }
        )
        response = Mock()
        response.raise_for_status.return_value = None
        return response

    result = outbox.drain(
        base_url="http://strapi:3000/",
        token="SECRET",
        post=fake_post,
    )

    assert result == {"sent": 1, "failed": 0, "retained": 0}
    assert outbox.pending_count() == 0
    assert posts == [
        {
            "url": "http://strapi:3000/cards/fromDB",
            "json": [501, 502],
            "headers": {
                "Authorization": "Bearer SECRET",
                "X-Strapi-Token": "SECRET",
            },
            "timeout": 60,
        }
    ]
    assert "SECRET" not in posts[0]["url"]
    assert "token=" not in posts[0]["url"]


def test_readiness_failure_retains_recovery_checkpoint_then_retries(monkeypatch):
    """A local readiness write failure cannot strand a pushed transaction."""
    import check_update as cu

    class Journal:
        transaction_id = "txn-ready-retry"
        push_order = []
        repos = {}
        phase = None
        deleted = False

        def set_phase(self, phase):
            self.phase = phase

        def delete(self):
            self.deleted = True

    journal = Journal()
    calls = []

    def mark_ready(transaction_id):
        calls.append(transaction_id)
        if len(calls) == 1:
            raise cu.StrapiOutboxError("injected readiness fsync failure")

    monkeypatch.setattr(cu, "_mark_strapi_transaction_ready", mark_ready)
    monkeypatch.setattr(cu, "_clear_staging_dir_safe", lambda *args: None)

    assert cu._recover_push(journal) == "strapi_readiness_failed"
    assert journal.deleted is False

    assert cu._recover_push(journal) is None
    assert journal.deleted is True
    assert calls == ["txn-ready-retry", "txn-ready-retry"]


def test_get_splitted_master_data_uses_fetch_master_split(monkeypatch):
    import check_update as cu

    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, params=None):
            self.calls.append((method, params))
            if method == "master_split_paths":
                return ["suite/master/a", "suite/master/b"]
            return {"cards": [{"id": 1}], "events": [{"id": 2}]}

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


def test_fetch_simple_version_info_requires_cdn_version_for_cn_tw_kr(monkeypatch):
    monkeypatch.setattr(
        check_update, "check_update_versions_url", "http://example/versions"
    )
    payload = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}

    for region in ("cn", "tw", "kr"):
        monkeypatch.setattr(check_update, "pjsk_region", region)
        resp = Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        monkeypatch.setattr(check_update.requests, "get", lambda *a, r=resp, **k: r)

        with pytest.raises(RuntimeError, match="Invalid simple version info response"):
            check_update.fetch_simple_version_info()


def test_fetch_simple_version_info_accepts_without_cdn_version_for_jp_en(monkeypatch):
    monkeypatch.setattr(
        check_update, "check_update_versions_url", "http://example/versions"
    )
    payload = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}

    for region in ("jp", "en"):
        monkeypatch.setattr(check_update, "pjsk_region", region)
        resp = Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        monkeypatch.setattr(check_update.requests, "get", lambda *a, r=resp, **k: r)

        assert check_update.fetch_simple_version_info() == payload


def test_fetch_simple_version_info_accepts_cdn_version_for_cn_tw_kr(monkeypatch):
    monkeypatch.setattr(
        check_update, "check_update_versions_url", "http://example/versions"
    )
    payload = {
        "appVersion": "1",
        "dataVersion": "1",
        "assetVersion": "1",
        "cdnVersion": "1",
    }

    for region in ("cn", "tw", "kr"):
        monkeypatch.setattr(check_update, "pjsk_region", region)
        resp = Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        monkeypatch.setattr(check_update.requests, "get", lambda *a, r=resp, **k: r)

        assert check_update.fetch_simple_version_info() == payload


def test_validate_information_rejects_missing_informations_field(monkeypatch):
    """refresh_information indexes res["informations"], so it must be required."""
    monkeypatch.setattr(check_update, "pjsk_region", "jp")

    for response in ({}, {"userHomeBanners": []}, {"userInformations": []}):
        with pytest.raises(check_update.ResponseValidationError):
            check_update.validate_information(response)
