"""
Client for interacting with Project Sekai (Hatsune Miku) game servers.

Provides high-level API for game login, account management, and data fetching.
Handles encryption/decryption, version checking, rate limiting, and automatic
session token refresh.

Supported full API regions: 'jp' (Japan), 'en' (English), 'tw' (Taiwan),
                            'kr' (Korea). CN is supported only by the standalone
                            simplified checkUpdate process (see D-001).
"""

import logging
import random
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from time import sleep
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import requests

from accounts import (
    AccountCredential,
    AccountRegion,
    AccountRegistrationAdapter,
    JpEnCredential,
    TwKrCredential,
)
from config import Config
from game_auth import GameAuthenticationService
from game_protocol import GameProtocolTransport
from game_services import GameAPIService, PublicGameAPIService
from utils.constants import (
    app_id_regions,
    initial_api_headers,
    nuverse_master_data_base_url,
    pjsk_region,
)
from utils.crypto import decrypt_msgpack
from utils.deadline import DeadlineExceeded, bounded_timeout, current_deadline
from utils.get_app_ver import (
    get_app_ver_and_hash_en,
    get_app_ver_and_hash_jp,
    get_app_ver_qooapp,
)

logger = logging.getLogger(__name__)

type APIResponse = bytes | dict[str, Any] | None


class AuthTransitionKind(StrEnum):
    """Phases of one hidden authentication transaction."""

    ATTEMPT = "attempt"
    SUCCESS = "success"
    FAILURE = "failure"


class RetryPolicy(StrEnum):
    """Whether a logical game API operation may be repeated safely."""

    NEVER = "never"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True)
class AuthTransition:
    """Typed, paired lifecycle notification for hidden authentication."""

    transaction_id: int
    kind: AuthTransitionKind
    error: BaseException | None = None


class APIClient:
    """
    Client for interacting with Project Sekai game servers.

    Manages authentication, version tracking, and API communication.
    Automatically handles rate limiting, session token refresh, and
    encryption/decryption of game protocol messages.

    Attributes:
        region: Game region ('jp', 'en', 'cn', 'tw', 'kr')
        account_info: Dictionary with userId, credential, signature
        version_info: Dictionary with app/data/asset version numbers
        user_info: Logged-in user profile information
        rate_limited: Whether client is cooling down from rate limit
    """

    def __init__(
        self, region: str = pjsk_region, logger: logging.Logger = logger
    ) -> None:
        """
        Initialize API client for a specific region.

        Args:
            region: Game region code ('jp', 'en', 'cn', 'tw', 'kr')
            logger: Logger instance for this client
        """
        self._account_info: dict[str, Any] = {}
        self._version_info: dict[str, Any] = {}
        self._user_info: dict[str, Any] = {}
        self._region: str = ""
        self._master_split_paths: list[str] = []

        self.logger = logger
        self.lifecycle_callback: Callable[[AuthTransition], None] | None = None
        self._auth_transaction_id = 0
        self.region = region
        self.headers = deepcopy(initial_api_headers[region])
        self.protocol = GameProtocolTransport(region, self.headers, logger)
        self.rate_limited = False

    @property
    def account_info(self) -> dict[str, Any]:
        """Get account information."""
        return self._account_info

    @account_info.setter
    def account_info(self, data: dict[str, Any]) -> None:
        """Set account information."""
        self._account_info = data

    @property
    def version_info(self) -> dict[str, Any]:
        """Get version information."""
        return self._version_info

    @version_info.setter
    def version_info(self, data: dict[str, Any]) -> None:
        """Set version information."""
        self._version_info = data

    @property
    def user_info(self) -> dict[str, Any]:
        """Get user information."""
        return self._user_info

    @user_info.setter
    def user_info(self, data: dict[str, Any]) -> None:
        """Set user information."""
        self._user_info = data

    @property
    def region(self) -> str:
        """Get the region code."""
        return self._region

    @region.setter
    def region(self, data: str) -> None:
        """
        Set the region code and update headers accordingly.

        Args:
            data: Region code ('jp', 'en', 'cn', 'tw', 'kr')
        """
        self._region = data
        self.headers = deepcopy(initial_api_headers[data])
        if hasattr(self, "protocol"):
            self.protocol = GameProtocolTransport(data, self.headers, self.logger)

    @property
    def master_split_paths(self) -> list[str]:
        """Get master data split paths."""
        return self._master_split_paths

    @master_split_paths.setter
    def master_split_paths(self, data: list[str]) -> None:
        """Set master data split paths."""
        self._master_split_paths = data

    def init_cookie(self) -> None:
        """
        Initialize session cookie for the region.

        Performs POST request to get-cookie endpoint and stores
        resulting Set-Cookie header for subsequent requests.

        Raises:
            RuntimeError: If the cookie response is unsuccessful or incomplete
        """
        self.protocol.init_cookie()

    def _encrypt_request_body(self, method: str, body: str | dict) -> bytes | None:
        return self.protocol.encrypt_request_body(method, body)

    def _send_api_request(
        self,
        endpoint: str,
        method: str,
        data: bytes | None,
        request_id: str | None = None,
    ) -> requests.Response:
        return self.protocol.send(endpoint, method, data, request_id)

    def _decrypt_response_data(self, response: requests.Response) -> APIResponse:
        return self.protocol.decrypt_response(response)

    @staticmethod
    def _require_dict_response(response: APIResponse, endpoint: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise RuntimeError(f"Expected object response from {endpoint}")
        return response

    def _update_version_after_426(self) -> None:
        if self.region in ["jp"]:
            ver_data = get_app_ver_and_hash_jp()
            self.headers["x-app-version"] = ver_data["appVersion"]
            self.headers["x-app-hash"] = ver_data["appHash"]
            self.version_info["appHash"] = ver_data["appHash"]
        elif self.region in ["en"]:
            ver_data = get_app_ver_and_hash_en()
            self.headers["x-app-version"] = ver_data["appVersion"]
            self.headers["x-app-hash"] = ver_data["appHash"]
            self.version_info["appHash"] = ver_data["appHash"]
        else:
            ver_text = get_app_ver_qooapp(app_id_regions[self.region])
            self.headers["x-app-version"] = ver_text
        self.check_versions()
        if self.account_info:
            self.login()

    def _handle_http_error_retry(  # noqa: C901 - established retry decision table
        self,
        response: requests.Response | None,
        res_data: Any,
    ) -> bool:  # noqa: C901 - preserves the established HTTP retry decision table
        error_code = res_data.get("errorCode") if isinstance(res_data, dict) else None

        if (
            response is not None
            and response.status_code == 403
            and self.region == "jp"
            and error_code != "session_error"
        ):
            self.logger.warning("%s server rejected cookie, refreshing...", self.region)
            self.init_cookie()
            return True

        if response is not None and response.status_code == 426:
            self.logger.warning("%s server should update version info", self.region)
            transaction_id: int | None = None
            if self.account_info:
                transaction_id = self._begin_auth_transition()
            try:
                self._update_version_after_426()
            except Exception as error:
                if transaction_id is not None:
                    self._finish_auth_transition(transaction_id, error)
                raise
            if transaction_id is not None:
                self._finish_auth_transition(transaction_id)
            return True

        if (
            response is not None
            and response.status_code == 406
            and error_code == "rule_not_agreement"
        ):
            self.logger.warning("%s server should accept new agreement", self.region)
            transaction_id = None
            if self.account_info:
                transaction_id = self._begin_auth_transition()
            try:
                self.accept_agreement()
                if self.account_info:
                    self.login()
            except Exception as error:
                if transaction_id is not None:
                    self._finish_auth_transition(transaction_id, error)
                raise
            if transaction_id is not None:
                self._finish_auth_transition(transaction_id)
            return True

        if (
            response is not None
            and response.status_code == 403
            and error_code == "session_error"
        ):
            transaction_id = None
            if self.account_info:
                transaction_id = self._begin_auth_transition()
            try:
                if self.account_info:
                    self.login()
            except Exception as error:
                if transaction_id is not None:
                    self._finish_auth_transition(transaction_id, error)
                raise
            if transaction_id is not None:
                self._finish_auth_transition(transaction_id)
            return True

        return False

    def _begin_auth_transition(self) -> int:
        self._auth_transaction_id += 1
        transaction_id = self._auth_transaction_id
        self._notify_lifecycle(
            AuthTransition(transaction_id, AuthTransitionKind.ATTEMPT)
        )
        return transaction_id

    def _finish_auth_transition(
        self, transaction_id: int, error: BaseException | None = None
    ) -> None:
        kind = (
            AuthTransitionKind.FAILURE
            if error is not None
            else AuthTransitionKind.SUCCESS
        )
        self._notify_lifecycle(AuthTransition(transaction_id, kind, error))

    def _notify_lifecycle(self, event: AuthTransition) -> None:
        """Notify an owning process about one paired auth transaction."""
        if self.lifecycle_callback is not None:
            self.lifecycle_callback(event)

    def _find_current_version_info(
        self, all_ver_infos: list[dict], curr_app_ver: str
    ) -> tuple[dict, bool]:
        available = [
            ver_info
            for ver_info in all_ver_infos
            if ver_info["appVersion"] == curr_app_ver
            and ver_info["appVersionStatus"] == "available"
        ]
        if available:
            return available[0], False

        available = [
            ver_info
            for ver_info in all_ver_infos
            if ver_info["appVersionStatus"] == "available"
        ]
        if available:
            return available[0], True

        maintenance = [
            ver_info
            for ver_info in all_ver_infos
            if ver_info["appVersionStatus"] == "maintenance"
        ]
        if maintenance:
            return maintenance[0], True

        raise RuntimeError(f"{self.region} server failed to fetch valid version info")

    def _is_version_updated(self, curr_ver_info: dict[str, Any]) -> bool:
        return bool(
            (
                "dataVersion" in curr_ver_info
                and self.headers["x-data-version"] != curr_ver_info["dataVersion"]
            )
            or self.headers["x-asset-version"] != curr_ver_info["assetVersion"]
            or self.headers["x-app-version"] != curr_ver_info["appVersion"]
        )

    def _apply_new_version_info(self, curr_ver_info: dict[str, Any]) -> None:
        if "dataVersion" in curr_ver_info:
            self.headers["x-data-version"] = curr_ver_info["dataVersion"]
        self.headers["x-asset-version"] = curr_ver_info["assetVersion"]
        self.headers["x-app-version"] = curr_ver_info["appVersion"]
        if (
            self.headers.get("x-app-hash", None) is not None
            and "appHash" in curr_ver_info
        ):
            self.headers["x-app-hash"] = curr_ver_info["appHash"]
        elif self.region in ["jp"]:
            ver_data = get_app_ver_and_hash_jp()
            self.headers["x-app-version"] = ver_data["appVersion"]
            self.headers["x-app-hash"] = ver_data["appHash"]
            self.version_info["appHash"] = ver_data["appHash"]
        elif self.region in ["en"]:
            ver_data = get_app_ver_and_hash_en()
            self.headers["x-app-version"] = ver_data["appVersion"]
            self.headers["x-app-hash"] = ver_data["appHash"]
            self.version_info["appHash"] = ver_data["appHash"]

    def _authenticate(self) -> dict[str, Any]:
        self.headers.pop("x-session-token", None)
        credential: AccountCredential
        if self.region in ("jp", "en"):
            credential = JpEnCredential(
                AccountRegion(self.region),
                str(self.account_info["userId"]),
                str(self.account_info["credential"]),
                str(self.account_info["signature"]),
            )
        elif self.region in ("tw", "kr"):
            device_id = self.account_info["deviceId"]
            if not isinstance(device_id, str) or not device_id:
                raise ValueError("TW/KR account info requires a non-empty deviceId")
            self.headers["device_id"] = device_id
            credential = TwKrCredential(
                AccountRegion(self.region),
                str(self.account_info["userId"]),
                str(self.account_info["loginInfo"]["accessToken"]),
                device_id,
            )
        elif self.region == "cn":
            access_token = self.account_info["loginInfo"]["accessToken"]
            return self._require_dict_response(
                self.call_pjsk_api(
                    "/user/auth", "post", {"userID": 0, "accessToken": access_token}
                ),
                "/user/auth",
            )
        else:
            raise ValueError(f"Unsupported region: {self.region}")

        result = GameAuthenticationService(self).authenticate(credential)
        self.master_split_paths = list(result.master_split_paths)
        return result.data

    def _apply_auth_headers_and_version_info(self, auth_data: dict[str, Any]) -> None:
        session_token = auth_data["sessionToken"]
        app_ver = auth_data["appVersion"]
        data_ver = auth_data["dataVersion"]
        asset_ver = auth_data["assetVersion"]
        asset_hash = auth_data["assetHash"] if "assetHash" in auth_data else None
        multi_play_ver = auth_data["multiPlayVersion"]

        self.headers["x-session-token"] = session_token
        self.headers["x-app-version"] = app_ver
        self.headers["x-data-version"] = data_ver
        self.headers["x-asset-version"] = asset_ver

        self.logger.info(
            "login appVersion=%s dataVersion=%s assetVersion=%s",
            app_ver,
            data_ver,
            asset_ver,
        )

        if self.region in ("cn", "tw", "kr"):
            self.version_info = {
                "systemProfile": "production",
                "appVersion": app_ver,
                "multiPlayVersion": multi_play_ver,
                "dataVersion": data_ver,
                "assetVersion": asset_ver,
                "appHash": "",
                "assetHash": "",
                "appVersionStatus": "available",
                "cdnVersion": auth_data["cdnVersion"],
            }
            return

        self.version_info["appVersion"] = app_ver
        self.version_info["assetVersion"] = asset_ver
        self.version_info["dataVersion"] = data_ver
        self.version_info["assetHash"] = asset_hash
        self.version_info["multiPlayVersion"] = multi_play_ver

    def _complete_tutorial_if_needed(
        self, user_id: str, user_info: dict[str, Any]
    ) -> None:
        user_tutorial = user_info["userTutorial"]
        if user_tutorial["tutorialStatus"] == "start":
            self.logger.warning("tutorial is at start, set username first")
            self.call_pjsk_api(
                f"/user/{user_id}/tutorial", "patch", {"tutorialStatus": "opening_1"}
            )
            self.call_pjsk_api(
                f"/user/{user_id}",
                "patch",
                {"userGamedata": {"name": "\u30bb\u30ab\u30a4\u306e\u4f4f\u4eba"}},
            )
            user_tutorial["tutorialStatus"] = "opening_1"

        if user_tutorial["tutorialStatus"] == "end":
            return

        self.logger.debug("roll tutorial")
        steps = [
            "opening_1",
            "gameplay",
            "opening_2",
            "unit_select",
            "idol_opening",
            "summary",
            "end",
        ]
        for status in steps[steps.index(user_tutorial["tutorialStatus"]) + 1 :]:
            self.call_pjsk_api(
                f"/user/{user_id}/tutorial", "patch", {"tutorialStatus": status}
            )

    def _post_login_refresh(self, user_id: str) -> None:
        self.logger.debug("check user invitation")
        self.call_pjsk_api(f"/user/{user_id}/invitation", "get")

        self.logger.debug("refresh home login_bonus")
        self.call_pjsk_api(
            f"/user/{user_id}/home/refresh",
            "put",
            {"refreshableTypes": ["login_bonus"]},
        )

    def call_pjsk_api(
        self,
        endpoint: str,
        method: str = "get",
        body: str | dict = "",
        retry_policy: RetryPolicy | None = None,
    ) -> APIResponse:
        """
        Make an encrypted API call to the PJSK game server.

        Handles request/response encryption, session token management,
        automatic retry on specific error conditions (cookie refresh,
        version update, rate limiting, etc.).

        Args:
            endpoint: API endpoint path (e.g., "/user/profile")
            method: HTTP method ('get', 'post', 'put', 'patch')
            body: Request body (string or dict, will be encrypted)
            retry_policy: Explicit retry safety. Defaults to IDEMPOTENT for GET
                and NEVER for methods that can have side effects.

        Returns:
            Decrypted response data (bytes or dict)

        Raises:
            RuntimeError: If API call fails after all retries
            ValueError: If body type is not str or dict
        """
        if self.rate_limited:
            raise RuntimeError("Cooling down for rate limit...")

        normalized_method = method.lower()
        data = self._encrypt_request_body(normalized_method, body)
        policy = (
            RetryPolicy(retry_policy)
            if retry_policy is not None
            else (
                RetryPolicy.IDEMPOTENT
                if normalized_method == "get"
                else RetryPolicy.NEVER
            )
        )

        max_retries = Config.MAX_API_RETRIES if policy is RetryPolicy.IDEMPOTENT else 0
        request_id = str(uuid4())
        attempt = 0
        while True:
            r = None
            res_data: APIResponse = None
            try:
                r = self._send_api_request(
                    endpoint, normalized_method, data, request_id
                )

                if 300 <= r.status_code < 400:
                    raise requests.HTTPError(response=r)
                res_data = self._decrypt_response_data(r)
                r.raise_for_status()
                return res_data
            except requests.HTTPError:
                status_code = r.status_code if r is not None else "unknown"
                self.logger.error(
                    "Request PJSK api error, endpoint=%s, method=%s, "
                    "body=%s, status=%s",
                    endpoint,
                    method,
                    body,
                    status_code,
                )

                should_retry = policy is RetryPolicy.IDEMPOTENT and (
                    self._handle_http_error_retry(r, res_data)
                )

                transient = r is not None and (
                    r.status_code == 429 or 500 <= r.status_code < 600
                )
                if (should_retry or transient) and attempt < max_retries:
                    attempt += 1
                    self._wait_before_retry(r, attempt)
                    continue

                raise RuntimeError(
                    f"PJSK API request failed (HTTP {status_code})"
                ) from None
            except requests.RequestException as err:
                self.logger.error(
                    "Request PJSK api request exception, endpoint=%s, "
                    "method=%s, error=%s",
                    endpoint,
                    method,
                    err,
                )
                if attempt < max_retries:
                    attempt += 1
                    self._wait_before_retry(None, attempt)
                    continue
                raise RuntimeError("PJSK API request failed") from None

    @staticmethod
    def _retry_after_seconds(response: requests.Response | None) -> float | None:
        if response is None or response.status_code != 429:
            return None
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())

    def _wait_before_retry(
        self, response: requests.Response | None, attempt: int
    ) -> None:
        retry_after = self._retry_after_seconds(response)
        if retry_after is None:
            base = min(30.0, 2.0 ** (attempt - 1))
            delay = random.uniform(base * 0.5, base * 1.5)
        else:
            delay = retry_after

        deadline = current_deadline()
        if deadline is not None and delay >= deadline.remaining():
            raise DeadlineExceeded("Request deadline exceeded")

        rate_limited = response is not None and response.status_code == 429
        if rate_limited:
            self.rate_limited = True
        try:
            sleep(delay)
        finally:
            if rate_limited:
                self.rate_limited = False

    def check_versions(
        self, input_ver_info: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Check and update game version information.

        Fetches current version info from server and updates internal
        headers if newer versions are available.

        Args:
            input_ver_info: Optional version info to compare against

        Returns:
            Dictionary with 'maintenance' and 'new_version' boolean flags
        """
        res = {"maintenance": False, "new_version": False}

        if self.region in ("cn", "tw", "kr"):
            return res

        system_data = self.fetch_system_data()
        if self.region in ("jp", "en"):
            res["maintenance"] = system_data["maintenanceStatus"] == "maintenance_in"

        all_ver_infos = system_data["appVersions"]
        curr_app_ver = self.headers["x-app-version"]
        curr_ver_info, fallback_selected = self._find_current_version_info(
            all_ver_infos, curr_app_ver
        )
        if curr_ver_info["appVersionStatus"] == "maintenance" and fallback_selected:
            res["maintenance"] = True
            res["new_version"] = False
        elif fallback_selected:
            res["new_version"] = True
        else:
            res["new_version"] = self._is_version_updated(curr_ver_info)

        if res["new_version"]:
            self._apply_new_version_info(curr_ver_info)

            self.logger.info(
                "%s server fetched a new available version: "
                "appVersion=%s, appHash=%s dataVersion=%s, assetVersion=%s",
                self.region,
                self.headers["x-app-version"],
                self.headers.get("x-app-hash", "N/A"),
                self.headers.get("x-data-version", "N/A"),
                self.headers["x-asset-version"],
            )

        for key in curr_ver_info:
            self.version_info[key] = curr_ver_info[key]

        if input_ver_info:
            res["maintenance"] = self.version_info["appVersionStatus"] == "maintenance"
            res["new_version"] = (
                (
                    "dataVersion" in input_ver_info
                    and "dataVersion" in curr_ver_info
                    and input_ver_info["dataVersion"] != curr_ver_info["dataVersion"]
                )
                or input_ver_info["assetVersion"] != curr_ver_info["assetVersion"]
                or input_ver_info["appVersion"] != curr_ver_info["appVersion"]
            )

        return res

    def register_new_account(self) -> dict[str, Any]:
        """
        Register a new account on the game server.

        Returns:
            Account registration response including credential and signature
        """
        return AccountRegistrationAdapter(self).register_raw(AccountRegion(self.region))

    def login(self) -> dict[str, Any]:
        """
        Authenticate and log in the account.

        Performs authentication flow, retrieves session token,
        updates version info, handles tutorial, and fetches user profile.

        Returns:
            User profile dictionary
        """
        self.logger.info("simulate login process")
        self.logger.debug("do auth")
        auth_data = self._authenticate()
        self._apply_auth_headers_and_version_info(auth_data)

        self.logger.debug("get suite user")
        user_id = self.account_info["userId"]
        user_info = self.fetch_suite_user()

        self.logger.debug("check and skip tutorial")
        self._complete_tutorial_if_needed(user_id, user_info)
        self._post_login_refresh(user_id)

        self.user_info = user_info
        return user_info

    def refresh_master_split_paths(self) -> list[str]:
        """Refresh authentication metadata without running post-login user requests."""
        if self.region not in ("jp", "en"):
            raise ValueError("Split master paths are only available for jp and en")

        auth_data = self._authenticate()
        self._apply_auth_headers_and_version_info(auth_data)
        return self.master_split_paths

    def fetch_suite_user(self, update_user_info: bool = False) -> dict[str, Any]:
        res = GameAPIService(self, str(self.account_info["userId"])).fetch_suite_user()

        if update_user_info:
            self.user_info = res

        return res

    def fetch_user_profile(self, user_id: str) -> dict[str, Any]:
        return GameAPIService(
            self, str(self.account_info["userId"])
        ).fetch_user_profile(self.region, user_id)

    def fetch_user_event_ranking(
        self, target_user_id: str, event_id: int
    ) -> dict[str, Any]:
        return GameAPIService(
            self, str(self.account_info["userId"])
        ).fetch_user_event_ranking(target_user_id, event_id)

    def fetch_information(self):
        return PublicGameAPIService(self).fetch_information()

    def fetch_system_data(self) -> dict[str, Any]:
        return PublicGameAPIService(self).fetch_system_data()

    def fetch_event_rank_first_100(self, event_id: int) -> dict[str, Any]:
        return GameAPIService(
            self, str(self.account_info["userId"])
        ).fetch_event_rank_first_100(event_id)

    def fetch_event_rank_border(self, event_id: int) -> dict[str, Any]:
        return PublicGameAPIService(self).fetch_event_rank_border(event_id)

    def accept_agreement(self):
        return GameAPIService(self, str(self.account_info["userId"])).accept_agreement(
            str(self.account_info["credential"]),
        )

    def fetch_master_split(self, split_path: str) -> Any:
        """Fetch a single master-data split by path (GET only).

        Only allowlisted split paths (present in ``master_split_paths``) are
        permitted. This is the safe, scoped replacement for the generic
        ``call_pjsk_api("/<split>")`` passthrough.
        """
        if split_path not in self.master_split_paths:
            raise ValueError(
                f"Master split path {split_path!r} is not in the allowlist"
            )
        return self.call_pjsk_api(f"/{split_path}")

    def request_and_decrypt(
        self,
        url: str,
        method: str = "get",
        body: str | dict[str, Any] = "",
    ) -> Any:
        self._validate_request_and_decrypt_url(url, method, body)
        res = requests.request(
            method,
            url,
            data=body,
            timeout=bounded_timeout(Config.REQUEST_TIMEOUT),
            allow_redirects=False,
        )
        if 300 <= res.status_code < 400:
            raise RuntimeError(f"Master-data redirect refused (HTTP {res.status_code})")
        res.raise_for_status()

        return decrypt_msgpack(res.content)

    def _validate_request_and_decrypt_url(
        self, url: str, method: str, body: str | dict[str, Any]
    ) -> None:
        """Strictly allowlist ``request_and_decrypt`` targets.

        Only GET with an empty body is permitted, and only against the
        current region's Nuverse master-data base URL, requesting exactly
        ``<base_path>/master-data-<digits>.info`` (no query, fragment,
        userinfo, non-default port, encoded/raw traversal, or extra
        sub-directories). Anything else is rejected (fail-closed).
        """
        if method.lower() != "get":
            raise ValueError("request_and_decrypt only allows GET")
        if body:
            raise ValueError("request_and_decrypt only allows an empty body")

        from posixpath import normpath
        from urllib.parse import unquote

        base = nuverse_master_data_base_url.get(self.region)
        if not base:
            raise ValueError(
                f"No master-data base URL configured for region {self.region!r}"
            )

        self._check_request_host_and_port(url, base)
        self._check_request_path(url, base, normpath, unquote)

    def _check_request_host_and_port(self, url: str, base: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError("request_and_decrypt requires https")
        # No query, fragment or userinfo allowed.
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("request_and_decrypt URL must not carry query/fragment")
        expected = urlparse(base)
        if parsed.hostname != expected.hostname:
            raise ValueError(
                f"request_and_decrypt host {parsed.hostname!r} is not allowlisted"
            )
        if parsed.port is not None and parsed.port != 443:
            raise ValueError("request_and_decrypt only allows the default https port")

    def _check_request_path(self, url: str, base: str, normpath, unquote) -> None:
        parsed = urlparse(url)
        # Decode (to catch %2e%2e encoded traversal) then normalize.
        raw_path = unquote(parsed.path)
        norm_path = normpath(raw_path)
        base_path = normpath(urlparse(base).path.rstrip("/"))
        if not norm_path.startswith(base_path + "/"):
            raise ValueError("request_and_decrypt path is outside the allowlist")
        if ".." in norm_path:
            raise ValueError("request_and_decrypt path is outside the allowlist")
        # Must be exactly one level under base and a master-data-<digits>.info file.
        if norm_path.count("/") != base_path.count("/") + 1:
            raise ValueError("request_and_decrypt path is outside the allowlist")
        filename = norm_path.rsplit("/", 1)[-1]
        import re as _re

        if not _re.fullmatch(r"master-data-\d+\.info", filename):
            raise ValueError(
                "request_and_decrypt only allows master-data-<digits>.info"
            )
