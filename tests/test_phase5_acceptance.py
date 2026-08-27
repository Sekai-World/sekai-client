"""Tests for the Phase 5 read-only production acceptance tool.

These tests cover PM2 parsing/validation, health response validation, redacted
output, and exit semantics. They never touch real PM2 or network services.
"""

import importlib.util
import json
from pathlib import Path
from unittest import mock

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
