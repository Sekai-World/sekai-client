"""Authentication session service independent of API client lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from accounts.models import AccountCredential, JpEnCredential


class AuthenticationTransport(Protocol):
    def call_pjsk_api(
        self,
        endpoint: str,
        method: str = "get",
        body: str | dict[str, Any] = "",
    ) -> bytes | dict[str, Any] | None: ...


@dataclass(frozen=True)
class AuthenticationResult:
    data: dict[str, Any]
    master_split_paths: tuple[str, ...] = ()


class GameAuthenticationService:
    def __init__(self, transport: AuthenticationTransport) -> None:
        self._transport = transport

    def authenticate(self, credential: AccountCredential) -> AuthenticationResult:
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
                {
                    "userID": 0,
                    "accessToken": credential.access_token,
                    "deviceId": None,
                    "authTriggerType": "normal",
                },
            )
        if not isinstance(response, dict) or not response.get("sessionToken"):
            raise ValueError("Invalid credential validation response")

        raw_paths = response.get("suiteMasterSplitPath", ())
        if not isinstance(raw_paths, (list, tuple)):
            raise ValueError("Invalid credential validation response")
        return AuthenticationResult(response, tuple(str(path) for path in raw_paths))
