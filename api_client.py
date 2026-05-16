"""
Client for interacting with Project Sekai (Hatsune Miku) game servers.

Provides high-level API for game login, account management, and data fetching.
Handles encryption/decryption, version checking, rate limiting, and automatic
session token refresh.

Supported regions: 'jp' (Japan), 'en' (English), 'cn' (China),
                  'tw' (Taiwan), 'kr' (Korea)
"""

import logging
from copy import deepcopy
from time import sleep
from typing import Any
from uuid import uuid4

import requests

from config import Config
from utils.constants import (
    app_id_regions,
    base_pjsk_api_url,
    initial_api_headers,
    pjsk_cookie_post_url,
    pjsk_region,
)
from utils.crypto import decrypt, decrypt_msgpack, encrypt, encrypt_msgpack
from utils.get_app_ver import (
    get_app_ver_and_hash_en,
    get_app_ver_and_hash_jp,
    get_app_ver_qooapp,
)

logger = logging.getLogger(__name__)


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
        self.region = region
        self.headers = deepcopy(initial_api_headers[region])
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
            requests.RequestException: If cookie initialization fails
        """
        r = requests.post(
            pjsk_cookie_post_url[self.region], timeout=Config.REQUEST_TIMEOUT
        )
        self.headers["cookie"] = r.headers["set-cookie"]

    def _encrypt_request_body(self, method: str, body: str | dict) -> bytes | None:
        if method.lower() not in ("post", "put", "patch"):
            return None
        if isinstance(body, str):
            return encrypt(body)
        if isinstance(body, dict):
            return encrypt_msgpack(body)
        raise ValueError("body type should be str or dict.")

    def _send_api_request(
        self,
        endpoint: str,
        method: str,
        data: bytes | None,
    ) -> requests.Response:
        self.headers["x-request-id"] = str(uuid4())
        self.headers["content-type"] = "application/octet-stream"
        self.logger.debug(
            "request url=%s%s, method=%s, headers=%s",
            base_pjsk_api_url[self.region],
            endpoint,
            method.lower(),
            self.headers.items(),
        )
        response = requests.request(
            method=method.lower(),
            url=f"{base_pjsk_api_url[self.region]}{endpoint}",
            headers=self.headers,
            data=data,
            timeout=Config.REQUEST_TIMEOUT,
        )
        self.logger.debug(
            "response url=%s%s, method=%s, headers=%s, status=%s",
            base_pjsk_api_url[self.region],
            endpoint,
            method.lower(),
            response.headers.items(),
            response.status_code,
        )
        if response.headers.get("x-session-token", None):
            self.headers["x-session-token"] = response.headers["x-session-token"]
        return response

    def _decrypt_response_data(self, response: requests.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if not response.content or "octet-stream" not in content_type:
            return None
        try:
            return decrypt_msgpack(response.content)
        except Exception:
            return decrypt(response.content)

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

    def _handle_http_error_retry(
        self,
        response: requests.Response | None,
        res_data: Any,
    ) -> bool:
        content_type = response.headers.get("content-type", "") if response else ""
        content_type_l = content_type.lower()
        error_code = res_data.get("errorCode") if isinstance(res_data, dict) else None

        if (
            response is not None
            and response.status_code == 403
            and content_type_l.startswith("text/xml")
        ):
            self.logger.warning("%s server cookie expired, refreshing...", self.region)
            self.init_cookie()
            return True

        if response is not None and response.status_code == 429:
            self.logger.warning("%s server hits rate limit, sleep for 60s", self.region)
            self.rate_limited = True
            sleep(60.0)
            self.rate_limited = False
            return True

        if response is not None and response.status_code == 426:
            self.logger.warning("%s server should update version info", self.region)
            self._update_version_after_426()
            return True

        if (
            response is not None
            and response.status_code == 406
            and error_code == "rule_not_agreement"
        ):
            self.logger.warning("%s server should accept new agreement", self.region)
            self.accept_agreement()
            if self.account_info:
                self.login()
            return True

        if (
            response is not None
            and response.status_code == 403
            and error_code == "session_error"
        ):
            if self.account_info:
                self.login()
            return True

        return False

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
        return (
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
        if self.region in ("jp", "en"):
            user_id = self.account_info["userId"]
            credential = self.account_info["credential"]
            auth_data = self.call_pjsk_api(
                f"/user/{user_id}/auth?refreshUpdatedResources=False",
                "put",
                {"credential": credential},
            )
            self.master_split_paths = auth_data["suiteMasterSplitPath"]
            return auth_data

        access_token = self.account_info["loginInfo"]["accessToken"]
        return self.call_pjsk_api(
            "/user/auth", "post", {"userID": 0, "accessToken": access_token}
        )

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
        retry_after_error: bool = True,
    ) -> bytes | dict[str, Any]:
        """
        Make an encrypted API call to the PJSK game server.

        Handles request/response encryption, session token management,
        automatic retry on specific error conditions (cookie refresh,
        version update, rate limiting, etc.).

        Args:
            endpoint: API endpoint path (e.g., "/user/profile")
            method: HTTP method ('get', 'post', 'put', 'patch')
            body: Request body (string or dict, will be encrypted)
            retry_after_error: Whether to retry on server errors

        Returns:
            Decrypted response data (bytes or dict)

        Raises:
            RuntimeError: If API call fails after all retries
            ValueError: If body type is not str or dict
        """
        if self.rate_limited:
            raise RuntimeError("Cooling down for rate limit...")

        data = self._encrypt_request_body(method, body)

        max_retries = Config.MAX_API_RETRIES if retry_after_error else 0
        attempt = 0
        while True:
            r = None
            res_data: Any = None
            try:
                r = self._send_api_request(endpoint, method, data)
                res_data = self._decrypt_response_data(r)

                r.raise_for_status()
                return res_data
            except requests.HTTPError as err:
                status_code = r.status_code if r is not None else "unknown"
                self.logger.error(
                    "Request PJSK api error, endpoint=%s, method=%s, "
                    "body=%s, status=%s",
                    endpoint,
                    method,
                    body,
                    status_code,
                )

                should_retry = self._handle_http_error_retry(r, res_data)

                if should_retry and attempt < max_retries:
                    attempt += 1
                    continue

                raise RuntimeError(r, res_data) from err
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
                    continue
                raise RuntimeError(str(err)) from err

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
        return self.call_pjsk_api(
            "/user",
            "post",
            {
                "platform": "iOS",
                "deviceModel": "iPad13,16",
                "operatingSystem": "iPadOS 17.4",
            },
        )

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

    def fetch_suite_user(self, update_user_info: bool = False) -> dict:
        user_id = self.account_info["userId"]
        res = self.call_pjsk_api(f"/suite/user/{user_id}")

        if update_user_info:
            self.user_info = res

        return res

    def fetch_user_profile(self, user_id: str) -> dict:
        my_user_id = self.account_info["userId"]
        if self.region == "jp":
            return self.call_pjsk_api(f"/user/{my_user_id}/{user_id}/profile")
        return self.call_pjsk_api(f"/user/{user_id}/profile")

    def fetch_user_event_ranking(self, target_user_id: str, event_id: str) -> dict:
        user_id = self.account_info["userId"]
        return self.call_pjsk_api(
            f"/user/{user_id}/event/{event_id}/ranking?targetUserId={target_user_id}"
        )

    def fetch_information(self):
        return self.call_pjsk_api("/information")

    def fetch_system_data(self):
        return self.call_pjsk_api("/system")

    def fetch_event_rank_first_100(self, event_id: str) -> dict:
        user_id = self.account_info["userId"]
        return self.call_pjsk_api(
            f"/user/{user_id}/event/{event_id}/ranking?rankingViewType=top100"
        )

    def fetch_event_rank_border(self, event_id: str) -> dict:
        return self.call_pjsk_api(f"/event/{event_id}/ranking-border")

    def accept_agreement(self):
        user_id = self.account_info["userId"]
        credential = self.account_info["credential"]
        return self.call_pjsk_api(
            f"/user/{user_id}/rule-agreement",
            "post",
            {"credential": credential, "userId": 0},
        )

    def request_and_decrypt(
        self,
        url: str,
        method: str = "get",
        body: str | dict[str, Any] = "",
    ) -> dict[str, Any]:
        res = requests.request(method, url, data=body, timeout=Config.REQUEST_TIMEOUT)
        res.raise_for_status()

        return decrypt_msgpack(res.content)
