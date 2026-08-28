"""Unit tests for lightweight API client metadata refresh."""

from unittest.mock import Mock

import pytest
import requests

import api_client
from api_client import APIClient, RetryPolicy
from game_auth import AuthenticationResult


def test_refresh_master_split_paths_only_applies_auth_metadata():
    client = Mock()
    client.region = "jp"
    client.master_split_paths = ["master/path"]
    auth_data = {"sessionToken": "new-token"}
    client._authenticate.return_value = auth_data

    result = APIClient.refresh_master_split_paths(client)

    assert result == ["master/path"]
    client._authenticate.assert_called_once_with()
    client._apply_auth_headers_and_version_info.assert_called_once_with(auth_data)
    client.fetch_suite_user.assert_not_called()


def test_refresh_master_split_paths_rejects_regions_without_split_data():
    client = Mock()
    client.region = "cn"

    try:
        APIClient.refresh_master_split_paths(client)
    except ValueError as error:
        assert "only available for jp and en" in str(error)
    else:
        raise AssertionError("Expected split path refresh to reject cn")


@pytest.mark.parametrize("region", ["jp", "en"])
def test_auth_metadata_preserves_app_hash_for_version_document(region):
    client = APIClient(region=region)
    client.headers["x-app-hash"] = "current-app-hash"

    client._apply_auth_headers_and_version_info(
        {
            "sessionToken": "session-token",
            "appVersion": "1.0.0",
            "dataVersion": "1.0.0.1",
            "assetVersion": "1.0.0.1",
            "multiPlayVersion": "miku",
        }
    )

    assert client.version_info["appHash"] == "current-app-hash"


def test_apply_new_version_info_preserves_valid_app_hash_header():
    client = APIClient(region="jp")
    client.headers["x-app-hash"] = "valid-hash"
    # Upstream system data may carry an empty appHash; that must not clobber
    # the already-validated header hash.
    client._apply_new_version_info(
        {
            "appVersion": "2.0.0",
            "dataVersion": "2.0.0.1",
            "assetVersion": "2.0.0.1",
            "appHash": "",
        }
    )
    assert client.headers["x-app-hash"] == "valid-hash"


def test_check_versions_preserves_valid_app_hash_over_empty_upstream(monkeypatch):
    client = APIClient(region="jp")
    client.headers["x-app-version"] = "1.0.0"
    client.headers["x-app-hash"] = "valid-hash"
    client.version_info = {
        "appVersion": "1.0.0",
        "dataVersion": "1.0.0.1",
        "assetVersion": "1.0.0.1",
        "appHash": "valid-hash",
    }

    system_data = {
        "maintenanceStatus": "available",
        "appVersions": [
            {
                "appVersion": "1.0.0",
                "dataVersion": "1.0.0.1",
                "assetVersion": "1.0.0.1",
                "appVersionStatus": "available",
                "appHash": "",
            }
        ],
    }
    monkeypatch.setattr(
        api_client.APIClient, "fetch_system_data", lambda self: system_data
    )

    client.check_versions()
    # An empty upstream appHash must not overwrite the existing valid value.
    assert client.version_info["appHash"] == "valid-hash"


def test_jp_403_refreshes_cookie_and_retries_without_xml_content_type(monkeypatch):
    client = APIClient(region="jp")
    rejected = Mock(spec=requests.Response)
    rejected.status_code = 403
    rejected.headers = {"content-type": "text/html"}
    rejected.content = b"Access denied"
    rejected.raise_for_status.side_effect = requests.HTTPError()
    accepted = Mock(spec=requests.Response)
    accepted.status_code = 200
    accepted.headers = {}
    accepted.content = b""
    accepted.raise_for_status.return_value = None

    client._send_api_request = Mock(side_effect=[rejected, accepted])
    client.init_cookie = Mock()

    assert client.call_pjsk_api("/system") is None
    client.init_cookie.assert_called_once_with()
    assert client._send_api_request.call_count == 2


def test_init_cookie_checks_status_and_stores_set_cookie(monkeypatch):
    response = Mock(status_code=200, headers={"set-cookie": "session-cookie"})
    monkeypatch.setattr(requests, "post", Mock(return_value=response))

    client = APIClient(region="jp")
    client.init_cookie()

    response.raise_for_status.assert_called_once_with()
    assert client.headers["cookie"] == "session-cookie"
    assert requests.post.call_args.kwargs["allow_redirects"] is False


def test_init_cookie_missing_set_cookie_is_bounded_error(monkeypatch):
    response = Mock(status_code=200, headers={})
    monkeypatch.setattr(requests, "post", Mock(return_value=response))

    with pytest.raises(RuntimeError, match="missing Set-Cookie"):
        APIClient(region="jp").init_cookie()

    response.raise_for_status.assert_called_once_with()


def test_init_cookie_rejects_redirect_without_following(monkeypatch):
    response = Mock(status_code=302, headers={"location": "https://secret.example"})
    monkeypatch.setattr(requests, "post", Mock(return_value=response))

    with pytest.raises(RuntimeError, match=r"redirect refused.*302"):
        APIClient(region="jp").init_cookie()

    response.raise_for_status.assert_not_called()
    assert requests.post.call_args.kwargs["allow_redirects"] is False


def test_init_cookie_request_error_does_not_expose_upstream_details(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        Mock(side_effect=requests.ConnectionError("upstream-secret-response")),
    )

    with pytest.raises(RuntimeError) as excinfo:
        APIClient(region="jp").init_cookie()

    assert str(excinfo.value) == "Cookie initialization request failed"
    assert "upstream-secret-response" not in str(excinfo.value)


def test_init_cookie_http_error_does_not_expose_upstream_details(monkeypatch):
    response = Mock(status_code=500, headers={})
    response.raise_for_status.side_effect = requests.HTTPError(
        "upstream-secret-response"
    )
    monkeypatch.setattr(requests, "post", Mock(return_value=response))

    with pytest.raises(RuntimeError) as excinfo:
        APIClient(region="jp").init_cookie()

    assert "500" in str(excinfo.value)
    assert "upstream-secret-response" not in str(excinfo.value)


def test_call_pjsk_api_http_error_does_not_expose_response_data():
    client = APIClient(region="jp")
    response = Mock(status_code=500)
    response.raise_for_status.side_effect = requests.HTTPError(
        "upstream-secret-response", response=response
    )
    client._send_api_request = Mock(return_value=response)
    client._decrypt_response_data = Mock(
        return_value={"errorCode": "upstream-secret-error"}
    )

    with pytest.raises(RuntimeError) as excinfo:
        client.call_pjsk_api("/system", retry_policy=RetryPolicy.NEVER)

    error = str(excinfo.value)
    assert error == "PJSK API request failed (HTTP 500)"
    assert "upstream-secret-response" not in error
    assert "upstream-secret-error" not in error


def test_redirect_does_not_persist_session_token_and_is_rejected(monkeypatch):
    client = APIClient(region="jp")
    client.headers["x-session-token"] = "existing-token"
    response = Mock(
        status_code=302,
        headers={
            "location": "https://malicious.example/",
            "x-session-token": "malicious-token",
        },
    )
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)

    with pytest.raises(RuntimeError, match=r"PJSK API request failed \(HTTP 302\)"):
        client.call_pjsk_api("/system", retry_policy=RetryPolicy.NEVER)

    assert client.headers["x-session-token"] == "existing-token"
    assert request.call_args.kwargs["allow_redirects"] is False


def test_non_redirect_response_persists_session_token(monkeypatch):
    client = APIClient(region="jp")
    response = Mock(status_code=200, headers={"x-session-token": "new-token"})
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)

    client._send_api_request("/system", "get", None)

    assert client.headers["x-session-token"] == "new-token"


def test_post_426_invokes_handler_but_does_not_replay_request(monkeypatch):
    """Regression: non-idempotent HTTP 426 must run recovery side effects."""
    client = APIClient(region="jp")
    response = Mock(status_code=426, headers={}, content=b"")
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(client, "_encrypt_request_body", lambda method, body: b"data")
    monkeypatch.setattr(api_client, "sleep", Mock())

    handler = Mock(return_value=True)
    client._handle_http_error_retry = handler

    with pytest.raises(RuntimeError, match="HTTP 426"):
        client.call_pjsk_api("/user", "post", {"action": "do"})

    handler.assert_called_once_with(response, None, endpoint="/user")
    assert request.call_count == 1


def test_auth_endpoint_426_skips_recursive_login(monkeypatch):
    """Regression: POST /user/auth 426 must not recurse via login()."""
    client = APIClient(region="kr")
    client.account_info = {"userId": "u"}
    login_mock = Mock()
    client.login = login_mock
    check_versions_mock = Mock()
    client.check_versions = check_versions_mock
    refresh_mock = Mock()
    client._refresh_tw_kr_asset_data_versions = refresh_mock

    response = Mock(status_code=426, headers={}, content=b"")
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(client, "_encrypt_request_body", lambda method, body: b"data")
    monkeypatch.setattr(api_client, "sleep", Mock())
    monkeypatch.setattr(
        api_client,
        "get_app_ver_qooapp",
        Mock(return_value="100.0"),
    )

    with pytest.raises(RuntimeError, match="HTTP 426"):
        client.call_pjsk_api("/user/auth", "post", {"userID": 0})

    check_versions_mock.assert_called_once()
    refresh_mock.assert_called_once()
    login_mock.assert_not_called()
    assert request.call_count == 1


def test_426_recovery_aborts_nested_version_recovery(monkeypatch):
    """Regression: JP/EN version recovery must not recurse on a nested 426."""
    client = APIClient(region="jp")
    response = Mock(status_code=426, headers={}, content=b"")
    original_handler = client._handle_http_error_retry
    nested_result = {}

    def _recover(*, endpoint=None):
        nested_result["handled"] = original_handler(response, None, endpoint=endpoint)

    client._update_version_after_426 = _recover

    assert original_handler(response, None, endpoint="/system") is True
    assert nested_result["handled"] is False
    assert client._recovering_426 is False


def test_tw_kr_426_refreshes_asset_and_data_versions(monkeypatch):
    """Regression: TW/KR 426 must refresh data/asset headers, not only app."""
    client = APIClient(region="kr")
    client.headers["x-data-version"] = "OLD_DATA"
    client.headers["x-asset-version"] = "OLD_ASSET"
    system_data = {
        "maintenanceStatus": "normal",
        "appVersions": [
            {
                "appVersion": "100.0",
                "appVersionStatus": "available",
                "dataVersion": "NEW_DATA",
                "assetVersion": "NEW_ASSET",
            },
        ],
    }
    call_mock = Mock(return_value=system_data)
    monkeypatch.setattr(client, "call_pjsk_api", call_mock)

    client._refresh_tw_kr_asset_data_versions()

    assert client.headers["x-data-version"] == "NEW_DATA"
    assert client.headers["x-asset-version"] == "NEW_ASSET"
    assert client.version_info["dataVersion"] == "NEW_DATA"
    assert client.version_info["assetVersion"] == "NEW_ASSET"
    call_mock.assert_called_once_with(
        "/system", "get", retry_policy=RetryPolicy.NEVER, bypass_error_recovery=True
    )


def test_tw_kr_426_fallback_synchronizes_app_version(monkeypatch):
    """Regression: when QooApp returns an unavailable version and /system
    selects a fallback, the app version fields must be synchronized too."""
    client = APIClient(region="kr")
    client.headers["x-app-version"] = "OLD_APP"
    client.headers["x-data-version"] = "OLD_DATA"
    client.headers["x-asset-version"] = "OLD_ASSET"
    client.version_info = {
        "appVersion": "OLD_APP",
        "dataVersion": "OLD_DATA",
        "assetVersion": "OLD_ASSET",
    }
    system_data = {
        "maintenanceStatus": "normal",
        "appVersions": [
            {
                "appVersion": "200.0",
                "appVersionStatus": "available",
                "dataVersion": "NEW_DATA",
                "assetVersion": "NEW_ASSET",
            },
        ],
    }
    call_mock = Mock(return_value=system_data)
    monkeypatch.setattr(client, "call_pjsk_api", call_mock)

    client._refresh_tw_kr_asset_data_versions()

    # The QooApp version had no available match, so /system fell back to a
    # different available version; the app version must follow along.
    assert client.headers["x-app-version"] == "200.0"
    assert client.version_info["appVersion"] == "200.0"
    assert client.headers["x-data-version"] == "NEW_DATA"
    assert client.version_info["dataVersion"] == "NEW_DATA"
    assert client.headers["x-asset-version"] == "NEW_ASSET"
    assert client.version_info["assetVersion"] == "NEW_ASSET"
    call_mock.assert_called_once_with(
        "/system", "get", retry_policy=RetryPolicy.NEVER, bypass_error_recovery=True
    )


def test_tw_kr_426_system_refresh_is_reentry_guarded(monkeypatch):
    """The system probe must not recurse if re-entered during 426 recovery."""
    client = APIClient(region="kr")
    client._refreshing_426_system = True
    spy = Mock()
    monkeypatch.setattr(client, "call_pjsk_api", spy)

    client._refresh_tw_kr_asset_data_versions()

    spy.assert_not_called()


def test_tw_kr_426_system_probe_bypasses_426_recovery(monkeypatch):
    """Regression: a 426 on the /system probe must not invoke version
    recovery or login. The probe's error recovery is bypassed entirely, so
    a 426 during the probe cannot recurse into ``_update_version_after_426``."""
    client = APIClient(region="kr")
    client.account_info = {"userId": "u"}
    update_426_mock = Mock()
    client._update_version_after_426 = update_426_mock
    login_mock = Mock()
    client.login = login_mock

    captured: dict = {}

    def _probe_426(endpoint, method="get", body="", retry_policy=None, **kwargs):
        if endpoint == "/system":
            captured.update(kwargs)
            raise RuntimeError("PJSK API request failed (HTTP 426)")
        raise AssertionError("unexpected endpoint during probe")

    client.call_pjsk_api = Mock(side_effect=_probe_426)
    monkeypatch.setattr(api_client, "get_app_ver_qooapp", Mock(return_value="100.0"))

    client._refresh_tw_kr_asset_data_versions()

    update_426_mock.assert_not_called()
    login_mock.assert_not_called()
    assert captured.get("bypass_error_recovery") is True


def test_tw_kr_426_system_refresh_survives_missing_app_versions(monkeypatch):
    """A malformed / no appVersions system response must not raise."""
    client = APIClient(region="kr")
    call_mock = Mock(return_value={"maintenanceStatus": "normal"})
    monkeypatch.setattr(client, "call_pjsk_api", call_mock)

    client._refresh_tw_kr_asset_data_versions()

    call_mock.assert_called_once()


def test_tw_kr_426_full_headers_refreshed_and_login_called(monkeypatch):
    """Non-auth TW/KR 426 refreshes full headers then re-logs in once."""
    client = APIClient(region="kr")
    client.account_info = {"userId": "u"}
    client.headers["x-data-version"] = "OLD_DATA"
    client.headers["x-asset-version"] = "OLD_ASSET"
    login_mock = Mock()
    client.login = login_mock
    check_versions_mock = Mock()
    client.check_versions = check_versions_mock

    system_data = {
        "maintenanceStatus": "normal",
        "appVersions": [
            {
                "appVersion": "100.0",
                "appVersionStatus": "available",
                "dataVersion": "NEW_DATA",
                "assetVersion": "NEW_ASSET",
            },
        ],
    }

    def _fake_call(endpoint, method="get", body="", retry_policy=None, **kwargs):
        if endpoint == "/system":
            return system_data
        raise AssertionError("unexpected call")

    client.call_pjsk_api = Mock(side_effect=_fake_call)
    monkeypatch.setattr(api_client, "get_app_ver_qooapp", Mock(return_value="100.0"))

    client._update_version_after_426(endpoint="/user/profile")

    assert client.headers["x-data-version"] == "NEW_DATA"
    assert client.headers["x-asset-version"] == "NEW_ASSET"
    check_versions_mock.assert_called_once()
    login_mock.assert_called_once()


@pytest.mark.parametrize(
    "auth_endpoint",
    [
        "/user/12345/auth?refreshUpdatedResources=False",
        "/user/auth",
    ],
)
def test_auth_endpoint_426_skips_login_jp_en(monkeypatch, auth_endpoint):
    """JP/EN auth endpoints must not recurse via login() on 426."""
    client = APIClient(region="jp")
    client.account_info = {"userId": "u"}
    login_mock = Mock()
    client.login = login_mock
    check_versions_mock = Mock()
    client.check_versions = check_versions_mock

    response = Mock(status_code=426, headers={}, content=b"")
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(client, "_encrypt_request_body", lambda method, body: b"data")
    monkeypatch.setattr(api_client, "sleep", Mock())
    monkeypatch.setattr(
        api_client,
        "get_app_ver_and_hash_jp",
        Mock(
            return_value={
                "appVersion": "100.0",
                "dataVersion": "100.0",
                "assetVersion": "100.0",
                "appHash": "abc",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="HTTP 426"):
        client.call_pjsk_api(auth_endpoint, "put", {"credential": "c"})

    check_versions_mock.assert_called_once()
    login_mock.assert_not_called()
    assert request.call_count == 1


def test_non_auth_426_still_calls_login_when_account_present(monkeypatch):
    """Non-auth endpoints must still re-login on 426.

    GET uses IDEMPOTENT policy, so the retry loop runs multiple times.
    We only need to verify login and check_versions were invoked.
    """
    client = APIClient(region="jp")
    client.account_info = {"userId": "u"}
    login_mock = Mock()
    client.login = login_mock
    check_versions_mock = Mock()
    client.check_versions = check_versions_mock

    response = Mock(status_code=426, headers={}, content=b"")
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(client, "_encrypt_request_body", lambda method, body: b"data")
    monkeypatch.setattr(api_client, "sleep", Mock())
    monkeypatch.setattr(
        api_client,
        "get_app_ver_and_hash_jp",
        Mock(
            return_value={
                "appVersion": "100.0",
                "dataVersion": "100.0",
                "assetVersion": "100.0",
                "appHash": "abc",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="HTTP 426"):
        client.call_pjsk_api("/user/profile", "get")

    check_versions_mock.assert_called()
    login_mock.assert_called()


def test_post_network_failure_is_not_retried(monkeypatch):
    client = APIClient(region="jp")
    request = Mock(side_effect=requests.ConnectionError("unknown outcome"))
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(client, "_encrypt_request_body", lambda method, body: b"data")
    monkeypatch.setattr(api_client, "sleep", Mock())

    with pytest.raises(RuntimeError, match="PJSK API request failed"):
        client.call_pjsk_api("/user", "post", {"platform": "iOS"})

    assert request.call_count == 1


def test_get_transient_failure_retries_with_same_request_id(monkeypatch):
    client = APIClient(region="jp")
    response = Mock(status_code=200, headers={}, content=b"")
    response.raise_for_status.return_value = None
    request = Mock(
        side_effect=[requests.ConnectionError("temporary failure"), response]
    )
    sleep_mock = Mock()
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(api_client, "sleep", sleep_mock)
    monkeypatch.setattr(api_client.random, "uniform", lambda low, high: 0.75)

    assert client.call_pjsk_api("/system") is None

    assert request.call_count == 2
    request_ids = [
        call.kwargs["headers"]["x-request-id"] for call in request.call_args_list
    ]
    assert len(set(request_ids)) == 1
    sleep_mock.assert_called_once_with(0.75)


def test_get_429_respects_retry_after(monkeypatch):
    client = APIClient(region="jp")
    limited = Mock(status_code=429, headers={"Retry-After": "2.5"}, content=b"")
    limited.raise_for_status.side_effect = requests.HTTPError(response=limited)
    success = Mock(status_code=200, headers={}, content=b"")
    success.raise_for_status.return_value = None
    request = Mock(side_effect=[limited, success])
    sleep_mock = Mock()
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(api_client, "sleep", sleep_mock)

    assert client.call_pjsk_api("/system") is None

    sleep_mock.assert_called_once_with(2.5)
    assert client.rate_limited is False


def test_retry_after_http_date_is_supported(monkeypatch):
    response = Mock(
        status_code=429,
        headers={"Retry-After": "Thu, 13 Aug 2026 12:00:05 GMT"},
    )
    fixed_now = api_client.datetime(2026, 8, 13, 12, 0, 0, tzinfo=api_client.UTC)
    datetime_mock = Mock()
    datetime_mock.now.return_value = fixed_now
    monkeypatch.setattr(api_client, "datetime", datetime_mock)

    assert APIClient._retry_after_seconds(response) == pytest.approx(5.0)


def test_non_transient_get_400_is_not_retried(monkeypatch):
    client = APIClient(region="jp")
    response = Mock(status_code=400, headers={}, content=b"")
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    request = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(api_client, "sleep", Mock())

    with pytest.raises(RuntimeError, match=r"HTTP 400"):
        client.call_pjsk_api("/system")

    assert request.call_count == 1


def test_jp_session_error_reauthenticates_instead_of_refreshing_cookie():
    client = APIClient(region="jp")
    client.account_info = {"userId": "user"}
    client.login = Mock()
    client.init_cookie = Mock()
    response = Mock(status_code=403)

    assert client._handle_http_error_retry(response, {"errorCode": "session_error"})
    client.login.assert_called_once_with()
    client.init_cookie.assert_not_called()


def test_session_error_during_login_skips_recursive_reauthentication(caplog):
    client = APIClient(region="en")
    client._authenticating = True
    response = Mock(status_code=403)

    assert not client._handle_http_error_retry(
        response, {"errorCode": "session_error"}, endpoint="/suite/user/account"
    )
    assert "endpoint_kind=suite_user" in caplog.text
    assert "skipping recursive login" in caplog.text


def test_login_clears_authentication_guard_after_failure():
    client = APIClient(region="en")
    client._authenticate = Mock(side_effect=RuntimeError("auth failed"))

    with pytest.raises(RuntimeError, match="auth failed"):
        client.login()

    assert client._authenticating is False


def test_hidden_auth_outcomes_are_paired_and_not_emitted_without_account():
    client = APIClient(region="jp")
    callback = Mock()
    client.lifecycle_callback = callback
    response = Mock(status_code=403)

    assert client._handle_http_error_retry(response, {"errorCode": "session_error"})
    callback.assert_not_called()

    client.account_info = {"userId": "user"}
    client.login = Mock()
    assert client._handle_http_error_retry(response, {"errorCode": "session_error"})
    events = [call.args[0] for call in callback.call_args_list]
    assert [event.kind for event in events] == [
        api_client.AuthTransitionKind.ATTEMPT,
        api_client.AuthTransitionKind.SUCCESS,
    ]
    assert events[0].transaction_id == events[1].transaction_id


def test_request_and_decrypt_allows_allowlisted_master_data_url(monkeypatch):
    client = APIClient(region="tw")
    resp = Mock(status_code=200, content=b"data")
    resp.raise_for_status.return_value = None
    monkeypatch.setattr(requests, "request", lambda *a, **k: resp)
    monkeypatch.setattr(api_client, "decrypt_msgpack", lambda c: c)

    # Nuverse base for tw is an https host serving master-data-<digits>.info
    from utils.constants import nuverse_master_data_base_url

    base = nuverse_master_data_base_url["tw"]
    url = f"{base}/master-data-60001.info"
    assert client.request_and_decrypt(url) == b"data"


def test_request_and_decrypt_rejects_redirect_without_following(monkeypatch):
    response = Mock(status_code=302, content=b"redirect-body")
    monkeypatch.setattr(requests, "request", Mock(return_value=response))
    client = APIClient(region="tw")

    from utils.constants import nuverse_master_data_base_url

    url = f"{nuverse_master_data_base_url['tw']}/master-data-60001.info"
    with pytest.raises(RuntimeError, match=r"redirect refused.*302"):
        client.request_and_decrypt(url)

    assert requests.request.call_args.kwargs["allow_redirects"] is False
    response.raise_for_status.assert_not_called()


def test_request_and_decrypt_rejects_non_get():
    client = APIClient(region="tw")
    with pytest.raises(ValueError, match="only allows GET"):
        client.request_and_decrypt("https://x/master-data-1.info", method="post")


def test_request_and_decrypt_rejects_non_https():
    client = APIClient(region="tw")
    with pytest.raises(ValueError, match="https"):
        client.request_and_decrypt("http://x/master-data-1.info")


def test_request_and_decrypt_rejects_wrong_host():
    client = APIClient(region="tw")
    with pytest.raises(ValueError, match="allowlisted"):
        client.request_and_decrypt("https://evil.example.com/master-data-1.info")


def test_request_and_decrypt_rejects_bad_filename():
    client = APIClient(region="tw")
    from utils.constants import nuverse_master_data_base_url

    base = nuverse_master_data_base_url["tw"]
    with pytest.raises(ValueError, match="outside the allowlist"):
        client.request_and_decrypt(f"{base}/../secret.txt")


def test_request_and_decrypt_rejects_traversal():
    client = APIClient(region="tw")
    from utils.constants import nuverse_master_data_base_url

    base = nuverse_master_data_base_url["tw"]
    with pytest.raises(ValueError, match="outside the allowlist"):
        client.request_and_decrypt(f"{base}/../../etc/passwd")


def test_fetch_master_split_rejects_unallowlisted():
    client = APIClient(region="jp")
    client.master_split_paths = ["suite/master/valid"]
    with pytest.raises(ValueError, match="not in the allowlist"):
        client.fetch_master_split("suite/master/evil")


def test_fetch_master_split_allows_and_calls():
    client = APIClient(region="jp")
    client.master_split_paths = ["suite/master/valid"]
    client.call_pjsk_api = Mock(return_value={"k": "v"})
    result = client.fetch_master_split("suite/master/valid")
    assert result == {"k": "v"}
    client.call_pjsk_api.assert_called_once_with("/suite/master/valid")


@pytest.mark.parametrize(
    "missing_key",
    ["deviceId", "installId", "userAgent", "deviceModel", "osVersion"],
)
def test_tw_kr_auth_rejects_missing_key_and_does_not_mutate_headers(missing_key):
    client = APIClient(region="tw")
    original_headers = dict(client.headers)
    client.account_info = {
        "userId": "u",
        "loginInfo": {"accessToken": "tok"},
        "deviceId": "device-id",
        "installId": "install-id",
        "userAgent": "user-agent",
        "deviceModel": "device-model",
        "osVersion": "os-version",
    }
    del client.account_info[missing_key]

    with pytest.raises(ValueError, match="missing keys"):
        client._authenticate()

    assert client.headers == original_headers


@pytest.mark.parametrize("region", ["jp", "en"])
def test_suite_auth_refreshes_current_version_headers(monkeypatch, region):
    client = APIClient(region=region)
    current = {
        "appVersion": "6.8.0",
        "dataVersion": "6.8.0.12",
        "assetVersion": "6.8.0.10",
        "appHash": "current-hash",
    }
    fetch = Mock(return_value=current)
    if region == "jp":
        monkeypatch.setattr(api_client, "get_app_ver_and_hash_jp", fetch)
    else:
        monkeypatch.setattr(api_client, "get_app_ver_and_hash_en", fetch)

    client._refresh_suite_version_headers()

    assert client.headers["x-app-version"] == current["appVersion"]
    assert client.headers["x-data-version"] == current["dataVersion"]
    assert client.headers["x-asset-version"] == current["assetVersion"]
    assert client.headers["x-app-hash"] == current["appHash"]
    fetch.assert_called_once_with()


@pytest.mark.parametrize("bad_device_id", [None, 0, False, "", 123])
def test_tw_kr_auth_rejects_malformed_device_id_and_does_not_mutate_headers(
    bad_device_id,
):
    client = APIClient(region="tw")
    original_headers = dict(client.headers)
    client.account_info = {
        "userId": "u",
        "loginInfo": {"accessToken": "tok"},
        "deviceId": bad_device_id,
        "installId": "install-id",
        "userAgent": "user-agent",
        "deviceModel": "device-model",
        "osVersion": "os-version",
    }

    with pytest.raises(ValueError, match="non-empty deviceId"):
        client._authenticate()

    assert client.headers == original_headers


@pytest.mark.parametrize(
    "field", ["installId", "userAgent", "deviceModel", "osVersion"]
)
@pytest.mark.parametrize("bad_value", [None, 0, False, "", 123])
def test_tw_kr_auth_rejects_malformed_fingerprint_field_and_does_not_mutate_headers(
    field, bad_value
):
    client = APIClient(region="tw")
    original_headers = dict(client.headers)
    client.account_info = {
        "userId": "u",
        "loginInfo": {"accessToken": "tok"},
        "deviceId": "device-id",
        "installId": "install-id",
        "userAgent": "user-agent",
        "deviceModel": "device-model",
        "osVersion": "os-version",
    }
    client.account_info[field] = bad_value

    with pytest.raises(ValueError, match=f"non-empty {field}"):
        client._authenticate()

    assert client.headers == original_headers


def test_tw_kr_auth_rejects_missing_cdn_version_without_mutating_split_paths(
    monkeypatch,
):
    client = APIClient(region="tw")
    client.account_info = {
        "userId": "u",
        "loginInfo": {"accessToken": "tok"},
        "deviceId": "device-id",
        "installId": "install-id",
        "userAgent": "user-agent",
        "deviceModel": "device-model",
        "osVersion": "os-version",
    }
    # Seed a sentinel so accidental mutation of split paths is detectable.
    client.master_split_paths = ["PREVIOUS"]

    class _FakeAuthService:
        def __init__(self, transport):
            self._transport = transport

        def authenticate(self, credential):
            # Common fields valid, but no cdnVersion -> TW/KR validation must fail.
            return AuthenticationResult(
                {
                    "sessionToken": "sess",
                    "appVersion": "1",
                    "dataVersion": "1",
                    "assetVersion": "1",
                    "multiPlayVersion": "1",
                },
                ("split/path",),
            )

    monkeypatch.setattr(api_client, "GameAuthenticationService", _FakeAuthService)

    with pytest.raises(RuntimeError, match="Invalid login response"):
        client._authenticate()

    # A malformed TW/KR auth response must not leave split paths mutated.
    assert client.master_split_paths == ["PREVIOUS"]


def test_tw_kr_auth_records_split_paths_only_after_cdn_version_validation(
    monkeypatch,
):
    client = APIClient(region="tw")
    client.account_info = {
        "userId": "u",
        "loginInfo": {"accessToken": "tok"},
        "deviceId": "device-id",
        "installId": "install-id",
        "userAgent": "user-agent",
        "deviceModel": "device-model",
        "osVersion": "os-version",
    }
    client.master_split_paths = ["PREVIOUS"]

    class _FakeAuthService:
        def __init__(self, transport):
            self._transport = transport

        def authenticate(self, credential):
            return AuthenticationResult(
                {
                    "sessionToken": "sess",
                    "appVersion": "1",
                    "dataVersion": "1",
                    "assetVersion": "1",
                    "multiPlayVersion": "1",
                    "cdnVersion": "1",
                },
                ("split/path",),
            )

    monkeypatch.setattr(api_client, "GameAuthenticationService", _FakeAuthService)

    auth_data = client._authenticate()

    assert auth_data["sessionToken"] == "sess"
    assert client.master_split_paths == ["split/path"]
