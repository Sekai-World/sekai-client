"""Contract tests for lifecycle-independent game-account registration."""

from unittest.mock import Mock

import pytest

from accounts import (
    AccountCredentialValidator,
    AccountRegion,
    AccountRegistrationAdapter,
    JpEnCredential,
    TwKrCredential,
)
from accounts.registration import REGISTRATION_PAYLOAD


def test_registration_uses_minimal_transport_and_returns_typed_credential(monkeypatch):
    transport = Mock()
    transport.call_pjsk_api.return_value = {
        "credential": "encoded-secret",
        "userRegistration": {"signature": "signature-secret"},
    }
    monkeypatch.setattr(
        "accounts.registration.jwt.decode",
        lambda credential, options: {"userId": 123},
    )

    credential = AccountRegistrationAdapter(transport).register(AccountRegion.JP)

    assert credential.region is AccountRegion.JP
    assert credential.user_id == "123"
    transport.call_pjsk_api.assert_called_once_with(
        "/user", "post", REGISTRATION_PAYLOAD
    )


def test_registration_raw_preserves_validated_legacy_response(monkeypatch):
    response = {
        "credential": "encoded-secret",
        "userRegistration": {"signature": "signature-secret"},
        "additionalField": "preserved",
    }
    transport = Mock()
    transport.call_pjsk_api.return_value = response
    monkeypatch.setattr(
        "accounts.registration.jwt.decode",
        lambda credential, options: {"userId": "123"},
    )

    assert (
        AccountRegistrationAdapter(transport).register_raw(AccountRegion.EN) is response
    )


@pytest.mark.parametrize("response", [None, b"data", {}, {"credential": "secret"}])
def test_registration_rejects_invalid_responses_without_secret_leak(
    monkeypatch, response
):
    transport = Mock()
    transport.call_pjsk_api.return_value = response
    monkeypatch.setattr(
        "accounts.registration.jwt.decode",
        lambda credential, options: {"userId": "user"},
    )

    with pytest.raises(ValueError) as caught:
        AccountRegistrationAdapter(transport).register(AccountRegion.EN)

    assert str(caught.value) == "Invalid account registration response"
    assert "secret" not in str(caught.value)


def test_registration_rejects_tw_before_transport_call():
    transport = Mock()

    with pytest.raises(ValueError, match="only jp and en"):
        AccountRegistrationAdapter(transport).register(AccountRegion.TW)

    transport.call_pjsk_api.assert_not_called()


@pytest.mark.parametrize(
    ("credential", "expected_call"),
    [
        (
            JpEnCredential(AccountRegion.JP, "user-1", "credential", "signature"),
            (
                "/user/user-1/auth?refreshUpdatedResources=False",
                "put",
                {"credential": "credential"},
            ),
        ),
        (
            TwKrCredential(AccountRegion.KR, "open-id", "access-token", "device-id"),
            (
                "/user/auth",
                "post",
                {
                    "userID": 0,
                    "accessToken": "access-token",
                    "deviceId": None,
                    "authTriggerType": "normal",
                },
            ),
        ),
    ],
)
def test_credential_validation_only_calls_auth_endpoint(credential, expected_call):
    transport = Mock()
    transport.call_pjsk_api.return_value = {"sessionToken": "session"}

    assert AccountCredentialValidator(transport).validate(credential) is True
    transport.call_pjsk_api.assert_called_once_with(*expected_call)


def test_credential_validation_rejects_malformed_response_without_secret_leak():
    transport = Mock()
    transport.call_pjsk_api.return_value = {"credential": "do-not-leak"}
    credential = JpEnCredential(
        AccountRegion.EN, "user", "credential-secret", "signature-secret"
    )

    with pytest.raises(ValueError) as caught:
        AccountCredentialValidator(transport).validate(credential)

    assert str(caught.value) == "Invalid credential validation response"
    assert "do-not-leak" not in str(caught.value)
