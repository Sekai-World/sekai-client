"""Tests for the lifecycle-independent authentication session service."""

from unittest.mock import Mock, call

import pytest

from accounts import AccountRegion, JpEnCredential, TwKrCredential
from game_auth import GameAuthenticationService


def _valid_auth_response(**overrides):
    response = {
        "sessionToken": "session",
        "appVersion": "1.0.0",
        "dataVersion": "1.0.0",
        "assetVersion": "1.0.0",
        "multiPlayVersion": "1.0.0",
    }
    response.update(overrides)
    return response


def _valid_tw_login_response(**overrides):
    response = _valid_auth_response(
        cdnVersion="20240101",
        appVersionStatus="available",
    )
    response.pop("sessionToken")
    response.update(overrides)
    return response


def test_jp_authentication_returns_session_metadata():
    transport = Mock()
    transport.call_pjsk_api.return_value = _valid_auth_response(
        suiteMasterSplitPath=["master/a"],
    )
    credential = JpEnCredential(AccountRegion.JP, "user", "credential", "signature")

    result = GameAuthenticationService(transport).authenticate(credential)

    assert result.master_split_paths == ("master/a",)
    transport.call_pjsk_api.assert_called_once_with(
        "/user/user/auth?refreshUpdatedResources=False",
        "put",
        {"credential": "credential"},
    )


@pytest.mark.parametrize("region", [AccountRegion.TW, AccountRegion.KR])
def test_tw_kr_authentication_uses_ordered_two_step_flow_and_hands_off_session_token(
    region,
):
    transport = Mock()
    transport.headers = {}

    def respond(endpoint, method="get", body=""):
        if endpoint == "/user/auth":
            return {"userId": 12345, "sessionToken": "initial-session"}
        assert endpoint == "/user/12345/login"
        assert method == "post"
        assert body == ""
        assert transport.headers["x-session-token"] == "initial-session"
        return _valid_tw_login_response()

    transport.call_pjsk_api.side_effect = respond
    credential = TwKrCredential(
        region,
        "open-id",
        "access-token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "os-version",
    )

    result = GameAuthenticationService(transport).authenticate(credential)

    assert transport.call_pjsk_api.call_args_list == [
        call("/user/auth", "post", {"accessToken": "access-token"}),
        call("/user/12345/login", "post"),
    ]
    assert result.data["sessionToken"] == "initial-session"
    assert result.data["appVersionStatus"] == "available"
    assert result.canonical_user_id == 12345


@pytest.mark.parametrize("response", [None, b"data", {}, {"sessionToken": ""}])
def test_authentication_rejects_invalid_response(response):
    transport = Mock()
    transport.call_pjsk_api.return_value = response
    credential = TwKrCredential(
        AccountRegion.TW,
        "open-id",
        "access-token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "os-version",
    )

    with pytest.raises(ValueError, match="Invalid credential validation response"):
        GameAuthenticationService(transport).authenticate(credential)

    transport.call_pjsk_api.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        None,
        b"data",
        {},
        {"userId": True, "sessionToken": "session"},
        {"userId": 0, "sessionToken": "session"},
        {"userId": 1, "sessionToken": ""},
    ],
)
def test_tw_authentication_rejects_malformed_first_response_without_login(response):
    transport = Mock()
    transport.headers = {}
    transport.call_pjsk_api.return_value = response
    credential = TwKrCredential(
        AccountRegion.TW,
        "open-id",
        "access-token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "os-version",
    )

    with pytest.raises(ValueError, match="Invalid credential validation response"):
        GameAuthenticationService(transport).authenticate(credential)

    transport.call_pjsk_api.assert_called_once_with(
        "/user/auth", "post", {"accessToken": "access-token"}
    )


@pytest.mark.parametrize(
    "login_response",
    [
        {},
        _valid_tw_login_response(appVersionStatus=""),
        _valid_tw_login_response(cdnVersion=None),
    ],
)
@pytest.mark.parametrize("region", [AccountRegion.TW, AccountRegion.KR])
def test_tw_kr_authentication_rejects_malformed_second_response(region, login_response):
    transport = Mock()
    transport.headers = {}
    transport.call_pjsk_api.side_effect = [
        {"userId": 12345, "sessionToken": "initial-session"},
        login_response,
    ]
    credential = TwKrCredential(
        region,
        "open-id",
        "access-token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "os-version",
    )

    with pytest.raises(
        ValueError, match=f"Invalid {region.value.upper()} login response"
    ):
        GameAuthenticationService(transport).authenticate(credential)

    assert transport.headers["x-session-token"] == "initial-session"
    assert transport.call_pjsk_api.call_args_list == [
        call("/user/auth", "post", {"accessToken": "access-token"}),
        call("/user/12345/login", "post"),
    ]


@pytest.mark.parametrize(
    ("region", "expected_device_id"),
    [("tw", "lease-device-id"), ("kr", "lease-device-id")],
)
def test_tw_kr_auth_sets_device_id_header_on_transport(
    monkeypatch, region, expected_device_id
):
    """TW and KR use the current credential's device ID after authentication."""
    from api_client import APIClient

    monkeypatch.delenv("SEKAI_TW_DEVICE_ID", raising=False)
    client = APIClient(region=region)
    client.account_info = {
        "userId": "open-id",
        "loginInfo": {"accessToken": "token"},
        "deviceId": "lease-device-id",
        "installId": "lease-install-id",
        "userAgent": "lease-user-agent",
        "deviceModel": "lease-device-model",
        "osVersion": "lease-os-version",
    }
    client.call_pjsk_api = Mock(
        side_effect=[
            {"userId": 12345, "sessionToken": "game-session"},
            _valid_tw_login_response(),
        ]
    )

    client._authenticate()

    assert client.headers["device_id"] == expected_device_id
    assert client.headers["x-install-id"] == "lease-install-id"
    assert client.headers["user-agent"] == "lease-user-agent"
    assert client.headers["x-devicemodel"] == "lease-device-model"
    os_header = "x-operatingSystem" if region == "tw" else "x-operatingsystem"
    assert client.headers[os_header] == "lease-os-version"
    client.call_pjsk_api.assert_has_calls(
        [
            call("/user/auth", "post", {"accessToken": "token"}),
            call("/user/12345/login", "post"),
        ]
    )
    assert client.headers["x-session-token"] == "game-session"


def test_tw_reauthentication_uses_new_lease_device_id(monkeypatch):
    from api_client import APIClient

    monkeypatch.delenv("SEKAI_TW_DEVICE_ID", raising=False)
    client = APIClient(region="tw")
    client.account_info = {
        "userId": "open-id",
        "loginInfo": {"accessToken": "token"},
        "deviceId": "first-lease-device-id",
        "installId": "lease-install-id",
        "userAgent": "lease-user-agent",
        "deviceModel": "lease-device-model",
        "osVersion": "lease-os-version",
    }
    device_ids_used_for_auth = []

    def respond(endpoint, method="get", body=""):
        if endpoint == "/user/auth":
            device_ids_used_for_auth.append(client.headers["device_id"])
            return {"userId": 12345, "sessionToken": "game-session"}
        assert endpoint == "/user/12345/login"
        return _valid_tw_login_response()

    client.call_pjsk_api = Mock(side_effect=respond)

    client._authenticate()
    client.account_info["deviceId"] = "second-lease-device-id"
    client._authenticate()

    assert device_ids_used_for_auth == [
        "first-lease-device-id",
        "second-lease-device-id",
    ]
    assert client.headers["device_id"] == "second-lease-device-id"


def test_jp_en_auth_sets_fingerprint_headers_from_lease(monkeypatch):
    """Verify _authenticate() sets all fingerprint headers from the lease for jp/en."""
    from api_client import APIClient

    client = APIClient(region="jp")
    monkeypatch.setattr(client, "_refresh_suite_version_headers", lambda: None)
    client.account_info = {
        "userId": "user",
        "credential": "cred",
        "signature": "sig",
        "installId": "lease-install-id",
        "xIf": "lease-if-id",
        "xKc": "lease-kc-id",
        "deviceModel": "lease-device-model",
        "osVersion": "lease-os-version",
        "userAgent": "lease-user-agent",
    }
    client.call_pjsk_api = Mock(
        return_value=_valid_auth_response(suiteMasterSplitPath=["master/a"])
    )

    client._authenticate()

    assert client.headers["x-install-id"] == "lease-install-id"
    assert client.headers["x-if"] == "lease-if-id"
    assert client.headers["x-kc"] == "lease-kc-id"
    assert client.headers["x-devicemodel"] == "lease-device-model"
    assert client.headers["x-operatingsystem"] == "lease-os-version"
    assert client.headers["user-agent"] == "lease-user-agent"
    client.call_pjsk_api.assert_called_once_with(
        "/user/user/auth?refreshUpdatedResources=False",
        "put",
        {"credential": "cred"},
    )


def test_jp_en_auth_without_lease_fingerprint_keeps_static_headers(monkeypatch):
    from api_client import APIClient

    client = APIClient(region="jp")
    monkeypatch.setattr(client, "_refresh_suite_version_headers", lambda: None)
    original_headers = dict(client.headers)
    client.account_info = {"userId": "user", "credential": "cred", "signature": "sig"}
    client.call_pjsk_api = Mock(
        return_value=_valid_auth_response(suiteMasterSplitPath=["master/a"])
    )

    client._authenticate()

    assert client.headers["x-install-id"] == original_headers["x-install-id"]
    assert client.headers["x-if"] == original_headers["x-if"]
    assert client.headers["x-kc"] == original_headers["x-kc"]
    assert client.headers["user-agent"] == original_headers["user-agent"]


def test_jp_en_auth_resets_fingerprint_headers_after_legacy_account(monkeypatch):
    """A legacy credential must not reuse the previous lease's device identity."""
    from api_client import APIClient

    client = APIClient(region="jp")
    monkeypatch.setattr(client, "_refresh_suite_version_headers", lambda: None)
    original_headers = dict(client.headers)
    client.call_pjsk_api = Mock(
        return_value=_valid_auth_response(suiteMasterSplitPath=["master/a"])
    )

    client.account_info = {
        "userId": "user",
        "credential": "cred",
        "signature": "sig",
        "installId": "lease-install-id",
        "xIf": "lease-if-id",
        "xKc": "lease-kc-id",
        "deviceModel": "lease-device-model",
        "osVersion": "lease-os-version",
        "userAgent": "lease-user-agent",
    }
    client._authenticate()
    assert client.headers["x-install-id"] == "lease-install-id"

    client.account_info = {
        "userId": "legacy-user",
        "credential": "legacy-cred",
        "signature": "legacy-sig",
    }
    client._authenticate()

    assert client.headers["x-install-id"] == original_headers["x-install-id"]
    assert client.headers["x-if"] == original_headers["x-if"]
    assert client.headers["x-kc"] == original_headers["x-kc"]
    assert client.headers["x-devicemodel"] == original_headers["x-devicemodel"]
    assert client.headers["x-operatingsystem"] == original_headers["x-operatingsystem"]
    assert client.headers["user-agent"] == original_headers["user-agent"]
    client.call_pjsk_api.assert_called_with(
        "/user/legacy-user/auth?refreshUpdatedResources=False",
        "put",
        {"credential": "legacy-cred"},
    )
