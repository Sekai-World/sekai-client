from unittest.mock import Mock

import pytest
import requests

from utils import get_app_ver
from utils.constants import EN_FALLBACK_VERSION_INFO, JP_FALLBACK_VERSION_INFO

EN_CURRENT_VERSION_URL = (
    "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-en-master/"
    "refs/heads/main/versions/current_version.json"
)
JP_CURRENT_VERSION_URL = (
    "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-master/"
    "refs/heads/main/versions/current_version.json"
)


def test_get_app_ver_qooapp_uses_bounded_success_checked_request(monkeypatch):
    response = requests.Response()
    response.status_code = 200
    response._content = b"""
        <div class="app-info android">
          <div class="row">label</div>
          <div class="row"><var>3.4.5</var></div>
        </div>
    """
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(get_app_ver.requests, "get", fake_get)
    monkeypatch.delenv("APP_VER", raising=False)

    assert get_app_ver.get_app_ver_qooapp("123") == "3.4.5"
    assert calls == [
        (
            "https://apps.qoo-app.com/en/app/123",
            {
                "headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:108.0) "
                        "Gecko/20100101 Firefox/108.0"
                    )
                },
                "timeout": 10,
            },
        )
    ]
    assert get_app_ver.environ["APP_VER"] == "3.4.5"


def test_get_app_ver_qooapp_raises_for_http_error_before_parsing(monkeypatch):
    response = requests.Response()
    response.status_code = 503
    response._content = b"not html"
    monkeypatch.setattr(get_app_ver.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises(requests.HTTPError):
        get_app_ver.get_app_ver_qooapp("123")


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("<html></html>", "Could not find QooApp version details"),
        (
            '<div class="app-info android"><div class="row">only</div></div>',
            "Could not find QooApp version row",
        ),
        (
            '<div class="app-info android"><div class="row">one</div>'
            '<div class="row">two</div></div>',
            "Could not find QooApp version value",
        ),
    ],
)
def test_get_app_ver_qooapp_preserves_dom_validation(monkeypatch, html, message):
    response = requests.Response()
    response.status_code = 200
    response._content = html.encode()
    monkeypatch.setattr(get_app_ver.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match=message):
        get_app_ver.get_app_ver_qooapp("123")


def test_get_app_ver_and_hash_en_uses_authoritative_bounded_request(monkeypatch):
    version = {
        "appVersion": "4.1.5",
        "dataVersion": "4.1.50.1",
        "assetVersion": "4.1.50.1",
        "appHash": "current-hash",
    }
    response = Mock(status_code=200)
    response.json.return_value = version
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(get_app_ver.requests, "get", fake_get)

    assert get_app_ver.get_app_ver_and_hash_en() == version
    assert calls == [(EN_CURRENT_VERSION_URL, {"timeout": 10})]
    response.raise_for_status.assert_called_once_with()


def test_get_app_ver_and_hash_en_uses_environment_url_override(monkeypatch):
    version = {
        "appVersion": "4.1.5",
        "dataVersion": "4.1.50.1",
        "assetVersion": "4.1.50.1",
        "appHash": "override-hash",
    }
    response = Mock(status_code=200)
    response.json.return_value = version
    request = Mock(return_value=response)
    override_url = "https://example.test/current_version.json"
    monkeypatch.setenv("EN_CURRENT_VERSION_URL", override_url)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_en() == version

    request.assert_called_once_with(override_url, timeout=10)


@pytest.mark.parametrize(
    "version",
    [
        [],
        {"appVersion": "4.1.5", "dataVersion": "4.1.50.1", "assetVersion": "4.1.50.1"},
        {
            "appVersion": "4.1.5",
            "dataVersion": "4.1.50.1",
            "assetVersion": "4.1.50.1",
            "appHash": "",
        },
    ],
)
def test_get_app_ver_and_hash_en_falls_back_for_invalid_authoritative_response(
    monkeypatch, version
):
    response = Mock(status_code=200)
    response.json.return_value = version
    request = Mock(return_value=response)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_en() == EN_FALLBACK_VERSION_INFO

    request.assert_called_once_with(EN_CURRENT_VERSION_URL, timeout=10)


def test_get_app_ver_and_hash_en_falls_back_on_malformed_json(monkeypatch):
    response = Mock(status_code=200)
    response.json.side_effect = ValueError("malformed JSON")
    request = Mock(return_value=response)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_en() == EN_FALLBACK_VERSION_INFO

    request.assert_called_once_with(EN_CURRENT_VERSION_URL, timeout=10)


def test_get_app_ver_and_hash_jp_uses_authoritative_bounded_request(monkeypatch):
    version = {
        "appVersion": "4.1.5",
        "dataVersion": "4.1.50.1",
        "assetVersion": "4.1.50.1",
        "appHash": "current-hash",
    }
    response = Mock(status_code=200)
    response.json.return_value = version
    request = Mock(return_value=response)
    monkeypatch.delenv("JP_CURRENT_VERSION_URL", raising=False)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_jp() == version

    request.assert_called_once_with(JP_CURRENT_VERSION_URL, timeout=10)
    response.raise_for_status.assert_called_once_with()


def test_get_app_ver_and_hash_jp_uses_environment_url_override(monkeypatch):
    version = {
        "appVersion": "4.1.5",
        "dataVersion": "4.1.50.1",
        "assetVersion": "4.1.50.1",
        "appHash": "override-hash",
    }
    response = Mock(status_code=200)
    response.json.return_value = version
    request = Mock(return_value=response)
    override_url = "https://example.test/current_version.json"
    monkeypatch.setenv("JP_CURRENT_VERSION_URL", override_url)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_jp() == version

    request.assert_called_once_with(override_url, timeout=10)


@pytest.mark.parametrize(
    "version",
    [
        [],
        {"appVersion": "4.1.5", "dataVersion": "4.1.50.1", "assetVersion": "4.1.50.1"},
        {
            "appVersion": "4.1.5",
            "dataVersion": "4.1.50.1",
            "assetVersion": "4.1.50.1",
            "appHash": "",
        },
    ],
)
def test_get_app_ver_and_hash_jp_falls_back_for_invalid_authoritative_response(
    monkeypatch, version
):
    response = Mock(status_code=200)
    response.json.return_value = version
    request = Mock(return_value=response)
    monkeypatch.delenv("JP_CURRENT_VERSION_URL", raising=False)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_jp() == JP_FALLBACK_VERSION_INFO

    request.assert_called_once_with(JP_CURRENT_VERSION_URL, timeout=10)


def test_get_app_ver_and_hash_jp_falls_back_on_malformed_json(monkeypatch):
    response = Mock(status_code=200)
    response.json.side_effect = ValueError("malformed JSON")
    request = Mock(return_value=response)
    monkeypatch.delenv("JP_CURRENT_VERSION_URL", raising=False)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_ver_and_hash_jp() == JP_FALLBACK_VERSION_INFO

    request.assert_called_once_with(JP_CURRENT_VERSION_URL, timeout=10)


@pytest.mark.parametrize(
    ("fetch", "url", "fallback", "url_env"),
    [
        (
            get_app_ver.get_app_ver_and_hash_en,
            EN_CURRENT_VERSION_URL,
            EN_FALLBACK_VERSION_INFO,
            "EN_CURRENT_VERSION_URL",
        ),
        (
            get_app_ver.get_app_ver_and_hash_jp,
            JP_CURRENT_VERSION_URL,
            JP_FALLBACK_VERSION_INFO,
            "JP_CURRENT_VERSION_URL",
        ),
    ],
)
def test_get_app_ver_and_hash_falls_back_on_network_error(
    monkeypatch, fetch, url, fallback, url_env
):
    request = Mock(side_effect=requests.Timeout("timed out"))
    monkeypatch.delenv(url_env, raising=False)
    monkeypatch.setenv("APP_VER", "stale-app-version")
    monkeypatch.setenv("APP_HASH", "stale-app-hash")
    monkeypatch.setenv("DATA_VER", "stale-data-version")
    monkeypatch.setenv("ASSET_VER", "stale-asset-version")
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert fetch() == fallback

    request.assert_called_once_with(url, timeout=10)


@pytest.mark.parametrize(
    ("fetch", "url", "fallback", "url_env"),
    [
        (
            get_app_ver.get_app_ver_and_hash_en,
            EN_CURRENT_VERSION_URL,
            EN_FALLBACK_VERSION_INFO,
            "EN_CURRENT_VERSION_URL",
        ),
        (
            get_app_ver.get_app_ver_and_hash_jp,
            JP_CURRENT_VERSION_URL,
            JP_FALLBACK_VERSION_INFO,
            "JP_CURRENT_VERSION_URL",
        ),
    ],
)
def test_get_app_ver_and_hash_falls_back_on_http_error(
    monkeypatch, fetch, url, fallback, url_env
):
    response = Mock(status_code=503)
    response.raise_for_status.side_effect = requests.HTTPError("service unavailable")
    request = Mock(return_value=response)
    monkeypatch.delenv(url_env, raising=False)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert fetch() == fallback

    request.assert_called_once_with(url, timeout=10)
    response.raise_for_status.assert_called_once_with()


APP_IDENTITY_KR_URL = (
    "https://raw.githubusercontent.com/Sekai-World/sekai-apphash-updater/"
    "refs/heads/data/KR.json"
)


def test_get_app_identity_reads_the_region_feed(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {
        "appVersion": "6.4.0",
        "appHash": "published-hash",
        "updatedAt": "2026-09-24T00:00:00Z",
    }
    request = Mock(return_value=response)
    monkeypatch.delenv("APP_IDENTITY_URL_TEMPLATE", raising=False)
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_identity("kr") == {
        "appVersion": "6.4.0",
        "appHash": "published-hash",
    }
    request.assert_called_once_with(APP_IDENTITY_KR_URL, timeout=10)
    response.raise_for_status.assert_called_once_with()


def test_get_app_identity_uses_environment_url_template(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"appVersion": "6.4.0", "appHash": "hash"}
    request = Mock(return_value=response)
    monkeypatch.setenv(
        "APP_IDENTITY_URL_TEMPLATE", "https://example.test/{region}.json"
    )
    monkeypatch.setattr(get_app_ver.requests, "get", request)

    assert get_app_ver.get_app_identity("tw") is not None
    request.assert_called_once_with("https://example.test/TW.json", timeout=10)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"appVersion": "6.4.0"},
        {"appVersion": "6.4.0", "appHash": ""},
        {"appVersion": 640, "appHash": "hash"},
    ],
)
def test_get_app_identity_rejects_malformed_payload(monkeypatch, payload):
    response = Mock(status_code=200)
    response.json.return_value = payload
    monkeypatch.setattr(get_app_ver.requests, "get", Mock(return_value=response))

    assert get_app_ver.get_app_identity("kr") is None


def test_get_app_identity_returns_none_on_http_error(monkeypatch):
    response = Mock(status_code=404)
    response.raise_for_status.side_effect = requests.HTTPError("not found")
    monkeypatch.setattr(get_app_ver.requests, "get", Mock(return_value=response))

    assert get_app_ver.get_app_identity("kr") is None
