"""Unit tests for lightweight API client metadata refresh."""

from unittest.mock import Mock

import pytest
import requests

import api_client
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
        client.call_pjsk_api("/system", retry_after_error=False)

    error = str(excinfo.value)
    assert error == "PJSK API request failed (HTTP 500)"
    assert "upstream-secret-response" not in error
    assert "upstream-secret-error" not in error


def test_jp_session_error_reauthenticates_instead_of_refreshing_cookie():
    client = APIClient(region="jp")
    client.account_info = {"userId": "user"}
    client.login = Mock()
    client.init_cookie = Mock()
    response = Mock(status_code=403)

    assert client._handle_http_error_retry(response, {"errorCode": "session_error"})
    client.login.assert_called_once_with()
    client.init_cookie.assert_not_called()


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
