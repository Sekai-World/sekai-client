"""High-level game API operations separated from transport and session state."""

from __future__ import annotations

from typing import Any, Protocol


class GameAPICaller(Protocol):
    def call_pjsk_api(
        self,
        endpoint: str,
        method: str = "get",
        body: str | dict[str, Any] = "",
    ) -> bytes | dict[str, Any] | None: ...


class GameAPIService:
    def __init__(self, caller: GameAPICaller, user_id: str) -> None:
        self._caller = caller
        self._user_id = user_id

    @staticmethod
    def _require_dict(response: object, endpoint: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise RuntimeError(f"Expected object response from {endpoint}")
        return response

    def fetch_suite_user(self) -> dict[str, Any]:
        endpoint = f"/suite/user/{self._user_id}"
        return self._require_dict(self._caller.call_pjsk_api(endpoint), endpoint)

    def fetch_user_profile(self, region: str, target_user_id: str) -> dict[str, Any]:
        endpoint = (
            f"/user/{self._user_id}/{target_user_id}/profile"
            if region == "jp"
            else f"/user/{target_user_id}/profile"
        )
        return self._require_dict(self._caller.call_pjsk_api(endpoint), endpoint)

    def fetch_user_event_ranking(
        self, target_user_id: str, event_id: int
    ) -> dict[str, Any]:
        endpoint = (
            f"/user/{self._user_id}/event/{event_id}/ranking"
            f"?targetUserId={target_user_id}"
        )
        return self._require_dict(self._caller.call_pjsk_api(endpoint), endpoint)

    def fetch_event_rank_first_100(self, event_id: int) -> dict[str, Any]:
        endpoint = (
            f"/user/{self._user_id}/event/{event_id}/ranking?rankingViewType=top100"
        )
        return self._require_dict(self._caller.call_pjsk_api(endpoint), endpoint)

    def accept_agreement(self, credential: str) -> object:
        return self._caller.call_pjsk_api(
            f"/user/{self._user_id}/rule-agreement",
            "post",
            {"credential": credential, "userId": 0},
        )


class PublicGameAPIService:
    """Game endpoints that do not require a user identifier."""

    def __init__(self, caller: GameAPICaller) -> None:
        self._caller = caller

    def fetch_information(self) -> object:
        return self._caller.call_pjsk_api("/information")

    def fetch_system_data(self) -> dict[str, Any]:
        return GameAPIService._require_dict(
            self._caller.call_pjsk_api("/system"), "/system"
        )

    def fetch_event_rank_border(self, event_id: int) -> dict[str, Any]:
        endpoint = f"/event/{event_id}/ranking-border"
        return GameAPIService._require_dict(
            self._caller.call_pjsk_api(endpoint), endpoint
        )
