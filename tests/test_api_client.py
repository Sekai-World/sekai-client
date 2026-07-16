"""Unit tests for lightweight API client metadata refresh."""

from unittest.mock import Mock

import requests

from api_client import APIClient


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


def test_jp_session_error_reauthenticates_instead_of_refreshing_cookie():
    client = APIClient(region="jp")
    client.account_info = {"userId": "user"}
    client.login = Mock()
    client.init_cookie = Mock()
    response = Mock(status_code=403)

    assert client._handle_http_error_retry(response, {"errorCode": "session_error"})
    client.login.assert_called_once_with()
    client.init_cookie.assert_not_called()
