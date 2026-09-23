from unittest.mock import Mock, call

import pytest

from utils.rpc_recovery import request_with_recovery


def test_success_does_not_call_recovery():
    client = Mock()
    client.request.return_value = {"ok": True}

    assert request_with_recovery(client, "check_versions") == {"ok": True}
    client.request.assert_called_once_with("check_versions")


def test_failure_recovers_once_and_retries():
    client = Mock()
    client.request.side_effect = [RuntimeError("rpc"), {"ready": True}, {"ok": True}]

    assert request_with_recovery(client, "version_info") == {"ok": True}
    assert client.request.call_args_list == [
        call("version_info"),
        call("ensure_ready"),
        call("version_info"),
    ]


@pytest.mark.parametrize("status", [{"ready": False}, None])
def test_recovery_requires_ready_status(status):
    client = Mock()
    client.request.side_effect = [RuntimeError("rpc"), status]

    with pytest.raises(RuntimeError, match="recovery failed"):
        request_with_recovery(client, "check_versions")

    assert client.request.call_count == 2


def test_ensure_ready_is_not_recursive():
    client = Mock()
    client.request.side_effect = RuntimeError("rpc")

    with pytest.raises(RuntimeError, match="rpc"):
        request_with_recovery(client, "ensure_ready")

    client.request.assert_called_once_with("ensure_ready")
