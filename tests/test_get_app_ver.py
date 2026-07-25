import pytest
import requests

from utils import get_app_ver


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
