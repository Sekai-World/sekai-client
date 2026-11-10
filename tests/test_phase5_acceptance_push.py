"""Tests for the Phase 5 acceptance push wrapper (no real network or PM2)."""

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

DEPLOYMENT = Path(__file__).resolve().parents[1] / "deployment"
sys.path.insert(0, str(DEPLOYMENT))
spec = importlib.util.spec_from_file_location(
    "phase5_acceptance_push", DEPLOYMENT / "phase5_acceptance_push.py"
)
assert spec is not None and spec.loader is not None
push_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_mod)

TOKEN_URL = "https://monitor.example/api/push/SECRET-TOKEN"


def _report(pm2="pass", gunicorn="pass", health="pass"):
    return {
        "status": "pass" if (pm2, gunicorn, health) == ("pass",) * 3 else "fail",
        "sections": {
            "pm2": {"status": pm2, "expected": 4, "present": 4, "online": 4},
            "gunicorn": {
                "status": gunicorn,
                "checked": 4,
                "workers_ok": 4,
                "bind_ok": 4,
                "config_ok": 4,
            },
            "health": {"status": health, "live": "pass", "ready": "pass"},
        },
    }


def test_build_message_is_aggregate_only_and_whitelisted():
    report = _report(health="fail")
    report["sections"]["pm2"]["leak"] = "secret-detail"
    assert push_mod.build_message(report) == "pm2=pass gunicorn=pass health=fail"
    weird = _report(pm2="http://evil.example/secret")
    assert push_mod.build_message(weird).startswith("pm2=fail ")
    assert push_mod.build_message({}) == "pm2=fail gunicorn=fail health=fail"


@pytest.mark.parametrize("is_up,expected", [(True, "up"), (False, "down")])
def test_build_push_url_sets_status_and_keeps_token(is_up, expected):
    url = push_mod.build_push_url(TOKEN_URL + "?status=up&msg=OK", is_up, "pm2=pass")
    assert url.startswith(TOKEN_URL + "?")
    assert f"status={expected}" in url
    assert "msg=pm2%3Dpass" in url
    assert url.count("status=") == 1


@pytest.mark.parametrize("bad", ["", "file:///etc/passwd", "ftp://x/y", "not a url"])
def test_build_push_url_rejects_non_http(bad):
    assert push_mod.build_push_url(bad, True, "m") is None


def _run(monkeypatch, capsys, exit_code, report, push_ok=True, env=TOKEN_URL):
    if env is None:
        monkeypatch.delenv(push_mod.PUSH_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(push_mod.PUSH_URL_ENV, env)
    sent = []

    def fake_push(url, timeout):
        sent.append(url)
        return push_ok

    with (
        mock.patch.object(
            push_mod.acceptance, "run_acceptance", return_value=(exit_code, report)
        ),
        mock.patch.object(push_mod, "push", side_effect=fake_push),
    ):
        code = push_mod.main([])
    return code, sent, capsys.readouterr()


def test_pass_pushes_up_and_returns_zero(monkeypatch, capsys):
    code, sent, out = _run(monkeypatch, capsys, 0, _report())
    assert code == 0
    assert len(sent) == 1 and "status=up" in sent[0]
    assert "SECRET-TOKEN" not in out.out + out.err


def test_failure_pushes_down_and_keeps_exit_code(monkeypatch, capsys):
    report = _report(health="fail")
    report["status"] = "fail"
    code, sent, out = _run(monkeypatch, capsys, 1, report)
    assert code == 1
    assert "status=down" in sent[0] and "health%3Dfail" in sent[0]
    assert "SECRET-TOKEN" not in out.out + out.err


def test_push_failure_returns_2_without_leaking(monkeypatch, capsys):
    code, _, out = _run(monkeypatch, capsys, 0, _report(), push_ok=False)
    assert code == push_mod.EXIT_PUSH_FAILED
    assert "SECRET-TOKEN" not in out.out + out.err


def test_missing_push_url_does_not_run_acceptance(monkeypatch, capsys):
    monkeypatch.delenv(push_mod.PUSH_URL_ENV, raising=False)
    with mock.patch.object(push_mod.acceptance, "run_acceptance") as run:
        assert push_mod.main([]) == push_mod.EXIT_PUSH_FAILED
        run.assert_not_called()


def test_unexpected_acceptance_error_pushes_down(monkeypatch, capsys):
    monkeypatch.setenv(push_mod.PUSH_URL_ENV, TOKEN_URL)
    sent = []
    with (
        mock.patch.object(
            push_mod.acceptance, "run_acceptance", side_effect=RuntimeError("secret")
        ),
        mock.patch.object(
            push_mod, "push", side_effect=lambda u, t: sent.append(u) or True
        ),
    ):
        assert push_mod.main([]) == 1
    out = capsys.readouterr()
    assert "status=down" in sent[0]
    assert "secret" not in out.out + out.err


def test_push_swallows_errors_and_checks_status():
    with mock.patch.object(push_mod._OPENER, "open", side_effect=OSError(TOKEN_URL)):
        assert push_mod.push(TOKEN_URL, 1.0) is False
    resp = mock.MagicMock()
    resp.__enter__.return_value.status = 200
    with mock.patch.object(push_mod._OPENER, "open", return_value=resp):
        assert push_mod.push(TOKEN_URL, 1.0) is True
    resp.__enter__.return_value.status = 500
    with mock.patch.object(push_mod._OPENER, "open", return_value=resp):
        assert push_mod.push(TOKEN_URL, 1.0) is False


def test_end_to_end_with_local_monitor_server(monkeypatch, capsys):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/push/tok"
        code, _, _ = _run_real_push(monkeypatch, capsys, url)
    finally:
        server.shutdown()
    assert code == 0
    assert seen and seen[0].startswith("/api/push/tok?status=up")


def _run_real_push(monkeypatch, capsys, url):
    monkeypatch.setenv(push_mod.PUSH_URL_ENV, url)
    with mock.patch.object(
        push_mod.acceptance, "run_acceptance", return_value=(0, _report())
    ):
        code = push_mod.main([])
    return code, None, capsys.readouterr()
