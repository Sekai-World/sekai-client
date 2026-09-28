"""Focused tests for the generic HTTP request execution boundary."""

from unittest.mock import Mock

import pytest
import requests

from utils.request_execution import RetryPolicy, execute_request


def _response(status_code: int) -> Mock:
    response = Mock(status_code=status_code, headers={}, content=b"")
    if 200 <= status_code < 300:
        response.raise_for_status.return_value = None
    else:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    return response


def test_executor_encrypts_once_and_reuses_request_id_on_retry():
    succeeded = _response(200)
    encrypt = Mock(return_value=b"encrypted")
    send = Mock(side_effect=[requests.ConnectionError("temporary"), succeeded])
    decrypt = Mock(return_value={"ok": True})
    recovery = Mock(return_value=False)
    wait = Mock()

    result = execute_request(
        "/logical-request",
        "post",
        {"value": 1},
        RetryPolicy.IDEMPOTENT,
        max_retries=1,
        logger=Mock(),
        encrypt_request_body=encrypt,
        send_request=send,
        decrypt_response=decrypt,
        recovery_handler=recovery,
        wait_for_retry=wait,
    )

    assert result == {"ok": True}
    encrypt.assert_called_once_with("post", {"value": 1})
    assert send.call_count == 2
    assert send.call_args_list[0].args[3] == send.call_args_list[1].args[3]
    wait.assert_called_once_with(None, 1)
    recovery.assert_not_called()
    decrypt.assert_called_once_with(succeeded)


def test_executor_runs_recovery_for_default_non_idempotent_call_without_replay():
    rejected = _response(426)
    send = Mock(return_value=rejected)
    decrypt = Mock(side_effect=ValueError("invalid error payload"))
    recovery = Mock(return_value=True)
    wait = Mock()

    with pytest.raises(RuntimeError, match="HTTP 426"):
        execute_request(
            "/side-effect",
            "post",
            "body",
            None,
            max_retries=3,
            logger=Mock(),
            encrypt_request_body=Mock(return_value=b"encrypted"),
            send_request=send,
            decrypt_response=decrypt,
            recovery_handler=recovery,
            wait_for_retry=wait,
        )

    recovery.assert_called_once_with(rejected, None)
    decrypt.assert_called_once_with(rejected)
    send.assert_called_once()
    wait.assert_not_called()


def test_executor_decodes_error_payload_before_recovery_and_replay():
    rejected = _response(406)
    succeeded = _response(200)
    responses = iter([rejected, succeeded])
    events = []

    def send(*args):
        events.append("send")
        return next(responses)

    error_payload = {"errorCode": "rule_not_agreement"}
    recovery = Mock(
        side_effect=lambda response, payload: events.append("recovery") or True
    )
    decrypt = Mock(side_effect=[error_payload, {"ok": True}])
    wait = Mock(side_effect=lambda *args: events.append("wait"))

    result = execute_request(
        "/logical-request",
        "get",
        "",
        RetryPolicy.IDEMPOTENT,
        max_retries=1,
        logger=Mock(),
        encrypt_request_body=Mock(return_value=None),
        send_request=send,
        decrypt_response=decrypt,
        recovery_handler=recovery,
        wait_for_retry=wait,
    )

    assert result == {"ok": True}
    recovery.assert_called_once_with(rejected, error_payload)
    assert events == ["send", "recovery", "wait", "send"]
    assert [call.args for call in decrypt.call_args_list] == [
        (rejected,),
        (succeeded,),
    ]


def test_executor_sanitizes_error_when_error_payload_decoding_fails():
    rejected = _response(406)
    send = Mock(return_value=rejected)
    decrypt = Mock(side_effect=ValueError("upstream decoder details"))
    recovery = Mock(return_value=False)

    with pytest.raises(RuntimeError, match="HTTP 406") as raised:
        execute_request(
            "/logical-request",
            "get",
            "",
            RetryPolicy.NEVER,
            max_retries=1,
            logger=Mock(),
            encrypt_request_body=Mock(return_value=None),
            send_request=send,
            decrypt_response=decrypt,
            recovery_handler=recovery,
            wait_for_retry=Mock(),
        )

    assert "upstream decoder details" not in str(raised.value)
    decrypt.assert_called_once_with(rejected)
    recovery.assert_called_once_with(rejected, None)
    send.assert_called_once()
