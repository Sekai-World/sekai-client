"""Tests for the Phase 5 read-only production acceptance tool.

These tests cover PM2 parsing/validation, health response validation, redacted
output, and exit semantics. They never touch real PM2 or network services.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "deployment" / "phase5_acceptance.py"
)
spec = importlib.util.spec_from_file_location("phase5_acceptance", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"cannot load module from {MODULE_PATH}")
phase5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phase5)


def _proc(
    name: str,
    *,
    online: bool = True,
    workers: str = "1",
    bind: str = "127.0.0.1:39390",
    config: str = "gunicorn_conf.py",
) -> dict:
    status = "online" if online else "stopped"
    script = (
        f"uv run gunicorn --config {config} --worker-class gevent "
        f"--workers {workers} --timeout 0 --bind {bind} shared_client:app"
    )
    return {"name": name, "pm2_env": {"status": status, "script": script}}


def _valid_jlist() -> str:
    entries = [
        _proc("sharedApiClient-jp"),
        _proc("sharedApiClient-en", bind="127.0.0.1:39392"),
        _proc("sharedApiClient-tw", bind="127.0.0.1:39391"),
        _proc("sharedApiClient-kr", bind="127.0.0.1:39393"),
        {
            "name": "checkUpdate-jp",
            "pm2_env": {"status": "online", "script": "uv run python check_update.py"},
        },
    ]
    return json.dumps(entries)


def test_parse_pm2_jlist_valid():
    entries = phase5.parse_pm2_jlist(_valid_jlist())
    assert len(entries) == 5
    selected = phase5.select_shared_client_processes(entries)
    assert set(selected) == {
        "sharedApiClient-jp",
        "sharedApiClient-en",
        "sharedApiClient-tw",
        "sharedApiClient-kr",
    }


def test_parse_pm2_jlist_malformed_raises():
    bad_cases = [
        "",
        "   ",
        "not json",
        "[1, 2, 3]",
        "{}",
        "[1, 2, {'x': 1}]",
        "not a list at all",
    ]
    for bad in bad_cases:
        try:
            phase5.parse_pm2_jlist(bad)
        except phase5.PM2ParseError:
            continue
        raise AssertionError(f"expected PM2ParseError for: {bad!r}")


def test_validate_processes_all_pass():
    entries = phase5.parse_pm2_jlist(_valid_jlist())
    proc = phase5.validate_processes(entries)
    assert proc["pm2"]["status"] == "pass"
    assert proc["gunicorn"]["status"] == "pass"
    assert proc["missing_count"] == 0
    assert proc["pm2"]["online"] == 4
    assert proc["gunicorn"]["workers_ok"] == 4


def test_validate_process_uses_pm2_script_and_split_args():
    entry = {
        "name": "sharedApiClient-jp",
        "pm2_env": {
            "status": "online",
            "script": "gunicorn",
            "args": [
                "--workers",
                "1",
                "--bind",
                "127.0.0.1:39390",
                "--config",
                "gunicorn_conf.py",
            ],
        },
    }

    result = phase5.validate_shared_client_process(entry["name"], entry)

    assert result == {
        "online": "pass",
        "workers": "pass",
        "bind": "pass",
        "config": "pass",
    }


def test_validate_process_uses_entry_script_and_split_args():
    entry = {
        "name": "sharedApiClient-jp",
        "script": "gunicorn",
        "args": "--workers 1 --bind 127.0.0.1:39390 --config gunicorn_conf.py",
        "pm2_env": {"status": "online"},
    }

    result = phase5.validate_shared_client_process(entry["name"], entry)

    assert result == {
        "online": "pass",
        "workers": "pass",
        "bind": "pass",
        "config": "pass",
    }


def test_validate_processes_missing_region():
    entries = phase5.parse_pm2_jlist(
        json.dumps([_proc("sharedApiClient-jp"), _proc("sharedApiClient-en")])
    )
    proc = phase5.validate_processes(entries)
    assert proc["pm2"]["status"] == "fail"
    assert proc["gunicorn"]["status"] == "fail"
    assert proc["missing_count"] == 2


def test_validate_process_not_online():
    entries = phase5.parse_pm2_jlist(
        json.dumps([_proc("sharedApiClient-jp", online=False)])
    )
    proc = phase5.validate_processes(entries)
    assert proc["pm2"]["status"] == "fail"
    assert proc["pm2"]["online"] == 0


def test_validate_process_workers_not_one():
    entries = phase5.parse_pm2_jlist(
        json.dumps([_proc("sharedApiClient-jp", workers="2")])
    )
    proc = phase5.validate_processes(entries)
    assert proc["gunicorn"]["status"] == "fail"
    assert proc["gunicorn"]["workers_ok"] == 0


def test_validate_process_bind_not_loopback():
    entries = phase5.parse_pm2_jlist(
        json.dumps([_proc("sharedApiClient-jp", bind="0.0.0.0:39390")])
    )
    proc = phase5.validate_processes(entries)
    assert proc["gunicorn"]["status"] == "fail"
    assert proc["gunicorn"]["bind_ok"] == 0


def _bind_ok(bind: str) -> bool:
    entries = phase5.parse_pm2_jlist(
        json.dumps([_proc("sharedApiClient-jp", bind=bind)])
    )
    proc = phase5.validate_processes(entries)
    return proc["gunicorn"]["bind_ok"] == 1


def test_loopback_bind_accepts_only_127_0_0_1():
    # Accepted: bare loopback and loopback with a valid port.
    assert _bind_ok("127.0.0.1")
    assert _bind_ok("127.0.0.1:39390")
    assert _bind_ok("127.0.0.1:1")
    assert _bind_ok("127.0.0.1:65535")

    # Rejected: different address that merely shares the prefix, and malformed
    # or out-of-range ports.
    assert not _bind_ok("127.0.0.10")
    assert not _bind_ok("127.0.0.1:0")
    assert not _bind_ok("127.0.0.1:70000")
    assert not _bind_ok("127.0.0.1:abc")
    assert not _bind_ok("127.0.0.1:")
    assert not _bind_ok("0.0.0.0:39390")
    assert not _bind_ok("127.0.0.2:39390")
    assert not _bind_ok("[::1]:39390")


def test_validate_processes_duplicate_name_fails():
    # Two entries sharing one expected process name must fail the expected-process
    # checks rather than silently overwriting each other.
    dup_entries = [
        _proc("sharedApiClient-jp", bind="127.0.0.1:39390"),
        _proc("sharedApiClient-jp", bind="127.0.0.1:39390"),
        _proc("sharedApiClient-en", bind="127.0.0.1:39392"),
        _proc("sharedApiClient-tw", bind="127.0.0.1:39391"),
        _proc("sharedApiClient-kr", bind="127.0.0.1:39393"),
    ]
    proc = phase5.validate_processes(phase5.parse_pm2_jlist(json.dumps(dup_entries)))
    assert proc["pm2"]["status"] == "fail"
    assert proc["gunicorn"]["status"] == "fail"
    assert proc["duplicate_count"] >= 1
    # Only four unique expected names are visible, so the duplicate is hidden
    # unless explicitly detected.
    assert proc["missing_count"] == 0


def test_run_acceptance_reports_duplicate_count():
    dup_entries = [
        _proc("sharedApiClient-jp"),
        _proc("sharedApiClient-jp"),
        _proc("sharedApiClient-en", bind="127.0.0.1:39392"),
        _proc("sharedApiClient-tw", bind="127.0.0.1:39391"),
        _proc("sharedApiClient-kr", bind="127.0.0.1:39393"),
    ]
    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout=json.dumps(dup_entries), stderr=""),
    ):
        _, report = phase5.run_acceptance(
            health_base_url="https://api.example.com",
            get=lambda url, timeout: (200, "{}"),
        )
    assert report["sections"]["duplicate_count"] == 1


def test_validate_process_config_missing():
    entries = phase5.parse_pm2_jlist(
        json.dumps([_proc("sharedApiClient-jp", config="other_conf.py")])
    )
    proc = phase5.validate_processes(entries)
    assert proc["gunicorn"]["status"] == "fail"
    assert proc["gunicorn"]["config_ok"] == 0


def test_validate_health_pass():
    def fake_get(url, timeout):
        return 200, '{"status":"success"}'

    health = phase5.validate_health("https://<host>", get=fake_get)
    assert health["status"] == "pass"
    assert health["live"] == "pass"
    assert health["ready"] == "pass"


def test_validate_health_live_failure():
    def fake_get(url, timeout):
        if url.endswith("/health/live"):
            return 500, "boom"
        return 200, "{}"

    health = phase5.validate_health("https://<host>", get=fake_get)
    assert health["status"] == "fail"
    assert health["live"] == "fail"
    assert health["ready"] == "pass"


def test_validate_health_ready_503():
    def fake_get(url, timeout):
        if url.endswith("/health/ready"):
            return 503, "{}"
        return 200, "{}"

    health = phase5.validate_health("https://<host>", get=fake_get)
    assert health["status"] == "fail"
    assert health["ready"] == "fail"


def test_validate_health_not_run_when_unset():
    health = phase5.validate_health("")
    assert health["status"] == "not_run"
    assert health["live"] == "not_run"
    assert health["ready"] == "not_run"


def test_validate_health_rejects_non_http_scheme():
    # A file:// URL must never be opened; treat unsupported schemes as fail.
    health = phase5.validate_health("file:///etc/passwd")
    assert health["status"] == "fail"
    assert health["live"] == "fail"
    assert health["ready"] == "fail"


def test_validate_health_requires_https_and_rejects_local_addresses():
    # Network must never be reached for any rejected URL.
    def fake_get(url, timeout):
        raise AssertionError(f"network request must not occur for {url}")

    loopback_and_private = [
        "http://host.example",  # not HTTPS
        "https://127.0.0.1",  # loopback literal
        "https://127.0.0.1:8080",  # loopback literal with port
        "https://[::1]",  # IPv6 loopback literal
        "https://10.0.0.1",  # RFC1918 private
        "https://192.168.1.1",  # RFC1918 private
        "https://172.16.5.5",  # RFC1918 private
        "https://169.254.1.1",  # link-local
        "https://0.0.0.0",  # unspecified
        "https://localhost",  # obvious localhost name
        "https://host.localhost",  # localhost suffix
        "https://host.local",  # mDNS local name
    ]
    for case in loopback_and_private:
        health = phase5.validate_health(case, get=fake_get)
        assert health["status"] == "fail", case
        assert health["live"] == "fail", case
        assert health["ready"] == "fail", case


def test_validate_health_http_requires_explicit_opt_in():
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return 200, "{}"

    denied = phase5.validate_health("http://api.example.com:9393", get=fake_get)
    assert denied["status"] == "fail"
    assert calls == []

    allowed = phase5.validate_health(
        "http://api.example.com:9393", get=fake_get, allow_http=True
    )
    assert allowed == {
        "status": "pass",
        "live": "pass",
        "ready": "pass",
        "transport": "http",
    }
    assert len(calls) == 2


_REJECTED_HOSTS = [
    "127.0.0.1:9393",  # loopback literal
    "[::1]",  # IPv6 loopback literal
    "10.0.0.1",  # RFC1918 private
    "192.168.1.1",  # RFC1918 private
    "172.16.5.5",  # RFC1918 private
    "169.254.169.254",  # link-local / metadata
    "0.0.0.0",  # unspecified
    "[::ffff:127.0.0.1]",  # IPv4-mapped loopback
    "[64:ff9b::7f00:1]",  # NAT64-embedded loopback
    "[2002:7f00:1::1]",  # 6to4-embedded loopback
    "127.1",  # non-canonical numeric IPv4
    "2130706433",  # decimal IPv4
    "0x7f000001",  # hex IPv4
    "0177.0.0.1",  # octal-part IPv4
    "0",  # numeric zero
    "localhost",
    "localhost.",  # trailing dot
    "LOCALHOST",
    "ip6-localhost",
    "host.localhost",
    "host.local",
    "foo.local.",
    "host.localdomain",
    "metadata.google.internal",
    "svc.internal.",
]


@pytest.mark.parametrize("scheme", ["https", "http"])
@pytest.mark.parametrize("host", _REJECTED_HOSTS)
def test_health_url_rejects_local_and_numeric_hosts(scheme, host):
    url = f"{scheme}://{host}"
    assert phase5._health_url_acceptable(url, allow_http=True) is False, url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host.example",
        "file:///etc/passwd",
        "http://user:secret-pass@host.example:9393",
        "http://:secret-pass@host.example:9393",
        "http://host.example:9393?token=secret",
        "http://host.example:9393?",  # empty query
        "http://host.example:9393#",  # empty fragment
        "http://host.example:9393/#secret",
        "https://host.example?",
        "https://host.example#",
        "http://",
        "//host.example",
    ],
)
def test_health_url_rejects_malformed_or_sensitive(url):
    assert phase5._health_url_acceptable(url, allow_http=True) is False, url


@pytest.mark.parametrize(
    "url",
    [
        "http://\u00b927.0.0.1:9393",  # superscript one, nameprep -> 127.0.0.1
        "http://1\u00ad27.0.0.1:9393",  # soft hyphen removed by nameprep
        "http://127.0.0.\u00b9:9393",
        "http://loc\u00adalhost:9393",
        "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54",
        "http://\u24db\u24de\u24d2\u24d0\u24db\u24d7\u24de\u24e2\u24e3",
        "http://local\u200bhost",  # zero-width space
        "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11",  # full-width digits
        "http://127\u30020\u30020\u30021",  # ideographic full stop
        "http://host%2e.example",  # percent-encoded host
        "http://host.example\\@evil.example",  # backslash userinfo trick
        "http://host .example",  # whitespace
    ],
)
def test_health_url_rejects_non_ascii_and_encoded_hosts(url):
    assert phase5._health_url_acceptable(url, allow_http=True) is False, url
    assert (
        phase5._health_url_acceptable(url.replace("http", "https", 1), allow_http=True)
        is False
    ), url


@pytest.mark.parametrize(
    "url",
    [
        "https://[secret-host.example]",
        "https://[127.0.0.1]",
        "https://[::1",
        "http://[::1]]",
    ],
)
def test_health_url_malformed_brackets_fail_without_raising(url):
    assert phase5._health_url_acceptable(url, allow_http=True) is False, url
    health = phase5.validate_health(
        url,
        get=lambda u, t: (_ for _ in ()).throw(AssertionError("no request")),
        allow_http=True,
    )
    assert health["status"] == "fail"


@pytest.mark.parametrize(
    "host",
    [
        "[::7f00:1]",  # IPv4-compatible
        "[::127.0.0.1]",
        "[::ffff:0:7f00:1]",  # SIIT IPv4-translated
        "[fec0::1]",  # deprecated site-local
        "224.0.0.1",  # IPv4 multicast
        "[ff02::1]",  # IPv6 multicast
        "svc.home.arpa",
    ],
)
def test_health_url_rejects_deprecated_and_multicast_ranges(host):
    assert phase5._health_url_acceptable(f"http://{host}", allow_http=True) is False


@pytest.mark.parametrize(
    "error",
    [
        phase5.http.client.BadStatusLine("SECRET-BODY-LINE token=abc"),
        phase5.http.client.LineTooLong("header line"),
        phase5.http.client.IncompleteRead(b"SECRET-BODY-LINE"),
    ],
)
def test_validate_health_malformed_response_is_fail_without_leak(error):
    def fake_get(url, timeout):
        raise error

    health = phase5.validate_health("https://api.example.com", get=fake_get)
    assert health["status"] == "fail"
    assert health["live"] == "fail"
    assert health["ready"] == "fail"
    assert "SECRET" not in json.dumps(health)


class _GarbageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.wfile.write(b"SECRET-BODY-LINE token=abc\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def test_http_get_malformed_response_raises_http_exception():
    server = HTTPServer(("127.0.0.1", 0), _GarbageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(phase5.http.client.HTTPException):
            phase5.http_get(f"http://127.0.0.1:{server.server_port}/x", 5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_get_ignores_environment_proxies(monkeypatch):
    # The opener is built at import time, so reload the module with proxy
    # variables set and check no environment proxy handler was installed.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    fresh_spec = importlib.util.spec_from_file_location("phase5_proxy", MODULE_PATH)
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    proxy_handlers = [
        h
        for h in fresh._OPENER.handlers
        if isinstance(h, fresh.urllib.request.ProxyHandler)
    ]
    assert all(not h.proxies for h in proxy_handlers)


def test_health_url_http_scheme_is_case_insensitive_but_still_opt_in():
    assert phase5._health_url_acceptable("HTTP://host.example") is False
    assert phase5._health_url_acceptable("HTTP://host.example", allow_http=True)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com",
        "https://api.example.com:8443",
        "https://8.8.8.8",
        "https://[2606:4700:4700::1111]",
        "https://api.example.com.",  # trailing dot on a public name
    ],
)
def test_health_url_accepts_public_hosts(url):
    assert phase5._health_url_acceptable(url) is True, url
    http_url = "http" + url[len("https") :]
    assert phase5._health_url_acceptable(http_url) is False
    assert phase5._health_url_acceptable(http_url, allow_http=True) is True


def test_validate_health_http_opt_in_never_requests_rejected_urls():
    def fake_get(url, timeout):
        raise AssertionError(f"network request must not occur for {url}")

    for host in _REJECTED_HOSTS:
        health = phase5.validate_health(f"http://{host}", get=fake_get, allow_http=True)
        assert health["status"] == "fail", host
        assert "transport" not in health, host


def test_run_acceptance_default_rejects_http_health():
    def fake_get(url, timeout):
        raise AssertionError("network request must not occur without opt-in")

    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout=_valid_jlist(), stderr=""),
    ):
        exit_code, report = phase5.run_acceptance(
            health_base_url="http://host.example:9393", get=fake_get
        )
    assert exit_code == 1
    assert report["sections"]["health"]["status"] == "fail"


def test_run_acceptance_http_opt_in_records_transport():
    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout=_valid_jlist(), stderr=""),
    ):
        exit_code, report = phase5.run_acceptance(
            health_base_url="http://host.example:9393",
            get=lambda u, t: (200, "{}"),
            allow_http_health=True,
        )
        https_code, https_report = phase5.run_acceptance(
            health_base_url="https://host.example",
            get=lambda u, t: (200, "{}"),
        )
    assert exit_code == 0
    assert report["sections"]["health"]["transport"] == "http"
    assert https_code == 0
    assert https_report["sections"]["health"]["transport"] == "https"
    assert "transport http" in phase5.format_report(report, "text")
    assert "host.example" not in phase5.format_report(report, "json")
    assert "host.example" not in phase5.format_report(report, "text")


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/health/live")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_http_get_refuses_redirects():
    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert phase5.http_get(base + "/health/live", 5.0)[0] == 200
        with pytest.raises(phase5.urllib.error.HTTPError) as excinfo:
            phase5.http_get(base + "/redirect", 5.0)
        assert excinfo.value.code == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_validate_health_redirect_is_fail():
    def fake_get(url, timeout):
        raise phase5.urllib.error.HTTPError(url, 302, "Found", {}, None)

    health = phase5.validate_health(
        "http://host.example:9393", get=fake_get, allow_http=True
    )
    assert health["status"] == "fail"
    assert health["live"] == "fail"
    assert health["ready"] == "fail"


def test_main_allow_http_health_flag(monkeypatch, capsys):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return 0, {
            "acceptance": "phase5",
            "status": "pass",
            "exit_code": 0,
            "sections": {
                "pm2": {"status": "pass", "expected": 4, "present": 4, "online": 4},
                "gunicorn": {
                    "status": "pass",
                    "checked": 4,
                    "workers_ok": 4,
                    "bind_ok": 4,
                    "config_ok": 4,
                },
                "health": {"status": "pass", "live": "pass", "ready": "pass"},
            },
        }

    monkeypatch.setattr(phase5, "run_acceptance", fake_run)
    assert phase5.main(["--health-base-url", "http://h.example:1"]) == 0
    assert seen["allow_http_health"] is False
    assert (
        phase5.main(["--health-base-url", "http://h.example:1", "--allow-http-health"])
        == 0
    )
    assert seen["allow_http_health"] is True
    capsys.readouterr()


def test_validate_health_local_success_still_fails():
    # Even a successful local response must fail: the URL is rejected before any
    # request, so `get` is never invoked.
    def fake_get(url, timeout):
        raise AssertionError("get must not be called for a rejected URL")

    health = phase5.validate_health("https://127.0.0.1/health", get=fake_get)
    assert health["status"] == "fail"
    assert health["live"] == "fail"
    assert health["ready"] == "fail"


def test_validate_health_public_hostname_passes():
    def fake_get(url, timeout):
        return 200, '{"status":"success"}'

    health = phase5.validate_health("https://api.example.com", get=fake_get)
    assert health["status"] == "pass"
    assert health["live"] == "pass"
    assert health["ready"] == "pass"


def test_validate_health_rejects_sensitive_or_malformed_urls():
    # No network call should be made for any of these; all must fail without
    # emitting the URL. Each case embeds a secret to prove it never appears.
    secret_cases = [
        "https://user:secret-pass@host.example/health",  # userinfo
        "https://host.example/health?token=secret",  # query
        "https://host.example/health#secret",  # fragment
        "https://",  # missing netloc
        "host.example",  # no scheme/netloc
        "//host.example",  # scheme-relative
    ]

    def fake_get(url, timeout):
        raise AssertionError(f"network request must not occur for {url}")

    for case in secret_cases:
        health = phase5.validate_health(case, get=fake_get)
        assert health["status"] == "fail", case
        assert health["live"] == "fail", case
        assert health["ready"] == "fail", case
        report = {
            "acceptance": "phase5",
            "status": "fail",
            "exit_code": 1,
            "sections": {
                "pm2": {"status": "fail", "expected": 4, "present": 0, "online": 0},
                "gunicorn": {
                    "status": "fail",
                    "checked": 0,
                    "workers_ok": 0,
                    "bind_ok": 0,
                    "config_ok": 0,
                },
                "missing_count": 4,
                "health": health,
            },
        }
        assert "secret" not in phase5.format_report(report, "text"), case
        assert "secret" not in phase5.format_report(report, "json"), case


def test_validate_health_network_error_is_fail():
    def fake_get(url, timeout):
        raise phase5.urllib.error.URLError("unreachable")

    health = phase5.validate_health("https://<host>", get=fake_get)
    assert health["status"] == "fail"
    assert health["live"] == "fail"
    assert health["ready"] == "fail"


def test_redacted_output_never_leaks_url_or_paths():
    secret_url = "https://secret-internal.example/health"
    body_with_secret = '{"status":"success","token":"topsecret"}'

    def fake_get(url, timeout):
        return 200, body_with_secret

    # Ensure no real PM2 process is ever invoked; return an empty jlist so the
    # pm2/gunicorn sections are exercised deterministically without side effects.
    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout="[]", stderr=""),
    ):
        exit_code, report = phase5.run_acceptance(
            health_base_url=secret_url,
            get=fake_get,
        )
    for fmt in ("text", "json"):
        rendered = phase5.format_report(report, fmt)
        assert secret_url not in rendered
        assert "topsecret" not in rendered
        assert "/health/live" not in rendered
        assert "/health/ready" not in rendered
        assert "127.0.0.1" not in rendered
        assert "pid" not in rendered.lower()


def test_exit_nonzero_when_pm2_missing():
    with mock.patch.object(
        phase5.subprocess, "run", side_effect=FileNotFoundError("pm2 missing")
    ):
        exit_code, report = phase5.run_acceptance(get=lambda u, t: (200, "{}"))
    assert exit_code == 1
    assert report["status"] == "fail"


def test_exit_zero_when_all_pass_and_health_run():
    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout=_valid_jlist(), stderr=""),
    ):
        exit_code, report = phase5.run_acceptance(
            health_base_url="https://<host>",
            get=lambda u, t: (200, "{}"),
        )
    assert exit_code == 0
    assert report["status"] == "pass"


def test_not_run_fails_without_flag():
    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout=_valid_jlist(), stderr=""),
    ):
        exit_code, report = phase5.run_acceptance(health_base_url="")
    assert exit_code == 1
    assert report["sections"]["health"]["status"] == "not_run"


def test_not_run_passes_with_allow_flag():
    with mock.patch.object(
        phase5.subprocess,
        "run",
        return_value=mock.Mock(stdout=_valid_jlist(), stderr=""),
    ):
        exit_code, report = phase5.run_acceptance(
            health_base_url="", allow_not_run=True
        )
    assert exit_code == 0
    assert report["status"] == "pass"
