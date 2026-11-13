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
assert spec is not None
assert spec.loader is not None
push_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_mod)

TOKEN_URL = "https://monitor.example/api/push/SECRET-TOKEN"
_ALL_PUSH_ENVS = [push_mod.PUSH_URL_ENV] + [
    push_mod.region_push_env(r) for r in push_mod.acceptance.FORMAL_REGIONS
]


@pytest.fixture(autouse=True)
def _clean_push_env(monkeypatch):
    for name in _ALL_PUSH_ENVS:
        monkeypatch.delenv(name, raising=False)


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
    assert len(sent) == 1
    assert "status=up" in sent[0]
    assert "SECRET-TOKEN" not in out.out + out.err


def test_failure_pushes_down_and_keeps_exit_code(monkeypatch, capsys):
    report = _report(health="fail")
    report["status"] = "fail"
    code, sent, out = _run(monkeypatch, capsys, 1, report)
    assert code == 1
    assert "status=down" in sent[0]
    assert "health%3Dfail" in sent[0]
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
    assert seen
    assert seen[0].startswith("/api/push/tok?status=up")


def _run_real_push(monkeypatch, capsys, url):
    monkeypatch.setenv(push_mod.PUSH_URL_ENV, url)
    with mock.patch.object(
        push_mod.acceptance, "run_acceptance", return_value=(0, _report())
    ):
        code = push_mod.main([])
    return code, None, capsys.readouterr()


def _regions_report(**overrides):
    report = _report()
    report["regions"] = {
        r: {"pm2": "pass", "gunicorn": "pass", "ready": "pass"}
        for r in push_mod.acceptance.FORMAL_REGIONS
    }
    for region, checks in overrides.items():
        report["regions"][region].update(checks)
    return report


def test_build_region_message_is_aggregate_only():
    report = _regions_report(tw={"ready": "fail"})
    report["regions"]["tw"]["leak"] = "secret-detail"
    assert push_mod.build_region_message(report, "tw") == (
        False,
        "pm2=pass gunicorn=pass ready=fail",
    )
    assert push_mod.build_region_message(report, "jp") == (
        True,
        "pm2=pass gunicorn=pass ready=pass",
    )
    assert push_mod.build_region_message({}, "kr") == (
        False,
        "pm2=fail gunicorn=fail ready=fail",
    )
    odd = _regions_report(jp={"pm2": "http://evil.example"})
    assert push_mod.build_region_message(odd, "jp")[1].startswith("pm2=fail ")
    not_run = _regions_report(en={"ready": "not_run"})
    assert push_mod.build_region_message(not_run, "en") == (
        False,
        "pm2=pass gunicorn=pass ready=not_run",
    )


def _run_regions(monkeypatch, report, exit_code, urls, fail_for=()):
    for name, url in urls.items():
        monkeypatch.setenv(name, url)
    sent = {}

    def fake_push(url, timeout):
        sent[url.split("?")[0]] = url
        return not any(token in url for token in fail_for)

    with (
        mock.patch.object(
            push_mod.acceptance, "run_acceptance", return_value=(exit_code, report)
        ),
        mock.patch.object(push_mod, "push", side_effect=fake_push),
    ):
        return push_mod.main([]), sent


def test_each_configured_region_gets_its_own_heartbeat(monkeypatch):
    urls = {
        push_mod.region_push_env("jp"): "https://m.example/api/push/jp-tok",
        push_mod.region_push_env("tw"): "https://m.example/api/push/tw-tok",
    }
    report = _regions_report(tw={"ready": "fail"})
    report["status"] = "fail"
    code, sent = _run_regions(monkeypatch, report, 1, urls)
    assert code == 1
    assert set(sent) == {
        "https://m.example/api/push/jp-tok",
        "https://m.example/api/push/tw-tok",
    }
    assert "status=up" in sent["https://m.example/api/push/jp-tok"]
    assert "status=down" in sent["https://m.example/api/push/tw-tok"]
    assert "ready%3Dfail" in sent["https://m.example/api/push/tw-tok"]


def test_overall_and_region_monitors_are_independent(monkeypatch):
    urls = {
        push_mod.PUSH_URL_ENV: "https://m.example/api/push/all-tok",
        push_mod.region_push_env("kr"): "https://m.example/api/push/kr-tok",
    }
    report = _regions_report(en={"pm2": "fail"})
    report["status"] = "fail"
    report["sections"]["pm2"]["status"] = "fail"
    code, sent = _run_regions(monkeypatch, report, 1, urls)
    assert code == 1
    assert "status=down" in sent["https://m.example/api/push/all-tok"]
    assert "pm2%3Dfail" in sent["https://m.example/api/push/all-tok"]
    assert "status=up" in sent["https://m.example/api/push/kr-tok"]


def test_one_failed_push_does_not_block_the_others(monkeypatch, capsys):
    urls = {
        push_mod.region_push_env("jp"): "https://m.example/api/push/jp-tok",
        push_mod.region_push_env("en"): "https://m.example/api/push/en-tok",
    }
    code, sent = _run_regions(
        monkeypatch, _regions_report(), 0, urls, fail_for=("jp-tok",)
    )
    assert code == push_mod.EXIT_PUSH_FAILED
    assert len(sent) == 2
    out = capsys.readouterr()
    assert "tok" not in out.out + out.err


def test_acceptance_error_marks_every_configured_region_down(monkeypatch):
    for region in push_mod.acceptance.FORMAL_REGIONS:
        monkeypatch.setenv(
            push_mod.region_push_env(region), f"https://m.example/api/push/{region}"
        )
    sent = []
    with (
        mock.patch.object(
            push_mod.acceptance, "run_acceptance", side_effect=RuntimeError("x")
        ),
        mock.patch.object(
            push_mod, "push", side_effect=lambda u, t: sent.append(u) or True
        ),
    ):
        assert push_mod.main([]) == 1
    assert len(sent) == 4
    assert all("status=down" in url for url in sent)
