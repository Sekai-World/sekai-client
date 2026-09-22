"""Authentication session service independent of API client lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from accounts.models import (
    AccountCredential,
    AccountRegion,
    JpEnCredential,
    TwKrCredential,
)
from response_models import (
    ResponseValidationError,
    validate_auth_response,
    validate_tw_auth_response,
    validate_tw_login_response,
)


class AuthenticationTransport(Protocol):
    headers: dict[str, Any]

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
        if (
            isinstance(credential, TwKrCredential)
            and credential.region == AccountRegion.TW
        ):
            return self._authenticate_tw(credential)

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
        # Validate the login response boundary before any session state is
        # derived from it. A malformed response raises a clear diagnostic error
        # instead of letting ``_apply_auth_headers_and_version_info`` fail later
        # with an opaque ``KeyError``/``TypeError``.
        try:
            validated = validate_auth_response(response)
        except ResponseValidationError as error:
            raise ValueError(
                f"Invalid credential validation response: {error}"
            ) from error

        raw_paths = validated.get("suiteMasterSplitPath", ())
        return AuthenticationResult(validated, tuple(str(path) for path in raw_paths))

    def _authenticate_tw(self, credential: TwKrCredential) -> AuthenticationResult:
        auth_response = self._transport.call_pjsk_api(
            "/user/auth",
            "post",
            {"accessToken": credential.access_token},
        )
        try:
            auth_data = validate_tw_auth_response(auth_response)
        except ResponseValidationError as error:
            raise ValueError(
                f"Invalid credential validation response: {error}"
            ) from error

        session_token = auth_data["sessionToken"]
        self._transport.headers["x-session-token"] = session_token
        login_response = self._transport.call_pjsk_api(
            f"/user/{auth_data['userId']}/login", "post"
        )
        try:
            login_data = validate_tw_login_response(login_response)
        except ResponseValidationError as error:
            raise ValueError(f"Invalid TW login response: {error}") from error

        result_data = dict(login_data)
        result_data["sessionToken"] = session_token
        return AuthenticationResult(result_data)
