"""Minimal game-account registration independent of client lifecycle code."""

from __future__ import annotations

from typing import Any, Protocol

import jwt

from accounts.models import AccountCredential, AccountRegion, JpEnCredential

REGISTRATION_PAYLOAD = {
    "platform": "iOS",
    "deviceModel": "iPad13,16",
    "operatingSystem": "iPadOS 17.4",
}


class RegistrationTransport(Protocol):
    """Smallest protocol surface required to register an account."""

    def call_pjsk_api(
        self,
        endpoint: str,
        method: str = "get",
        body: str | dict[str, Any] = "",
    ) -> bytes | dict[str, Any] | None: ...


class AccountRegistrationAdapter:
    def __init__(self, transport: RegistrationTransport) -> None:
        self._transport = transport

    def register(self, region: AccountRegion) -> JpEnCredential:
        return parse_registration_response(region, self.register_raw(region))

    def register_raw(self, region: AccountRegion) -> dict[str, Any]:
        """Return the validated original response for legacy callers."""
        if region not in (AccountRegion.JP, AccountRegion.EN):
            raise ValueError("Local game registration supports only jp and en")

        response = self._transport.call_pjsk_api(
            "/user", "post", dict(REGISTRATION_PAYLOAD)
        )
        if not isinstance(response, dict):
            raise ValueError("Invalid account registration response")
        parse_registration_response(region, response)
        return response


class AccountCredentialValidator:
    """Validate credentials through authentication without full game login."""

    def __init__(self, transport: RegistrationTransport) -> None:
        self._transport = transport

    def validate(self, credential: AccountCredential) -> bool:
        if isinstance(credential, JpEnCredential):
            response = self._transport.call_pjsk_api(
                f"/user/{credential.user_id}/auth?refreshUpdatedResources=False",
                "put",
                {"credential": credential.credential},
            )
        else:
            response = self._transport.call_pjsk_api(
                "/user/auth",
                "post",
                {"userID": 0, "accessToken": credential.access_token},
            )
        if not isinstance(response, dict) or not response.get("sessionToken"):
            raise ValueError("Invalid credential validation response")
        return True


def parse_registration_response(
    region: AccountRegion, response: dict[str, Any]
) -> JpEnCredential:
    """Validate a registration response without exposing credential values."""
    try:
        credential = response["credential"]
        registration = response["userRegistration"]
        if not isinstance(credential, str) or not isinstance(registration, dict):
            raise TypeError
        signature = registration["signature"]
        claims = jwt.decode(credential, options={"verify_signature": False})
        user_id = claims["userId"]
        if not isinstance(signature, str) or not isinstance(user_id, (str, int)):
            raise TypeError
        return JpEnCredential(region, str(user_id), credential, signature)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid account registration response") from error
