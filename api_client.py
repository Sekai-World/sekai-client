"""
Client for interacting with Project Sekai (Hatsune Miku) game servers.

Provides high-level API for game login, account management, and data fetching.
Handles encryption/decryption, version checking, rate limiting, and automatic
session token refresh.

Supported full API regions: 'jp' (Japan), 'en' (English), 'tw' (Taiwan),
                            'kr' (Korea). CN is supported only by the standalone
                            simplified checkUpdate process (see D-001).
"""

import json
import logging
import random
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from time import sleep
from typing import Any, TypeVar
from urllib.parse import urlparse

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
from response_models import (
    ResponseValidationError,
    validate_auth_response,
    validate_system_data,
    validate_version_info,
)
from utils.constants import (
    EN_FALLBACK_VERSION_INFO,
    JP_FALLBACK_VERSION_INFO,
    app_id_regions,
    initial_api_headers,
    nuverse_master_data_base_url,
    pjsk_region,
)
from utils.crypto import decrypt_msgpack
from utils.deadline import bounded_timeout
from utils.get_app_ver import (
    get_app_identity,
    get_app_ver_and_hash_en,
    get_app_ver_and_hash_jp,
    get_app_ver_qooapp,
)
from utils.request_execution import (
    APIResponse,
    RetryPolicy,
    execute_request,
)
from utils.request_execution import (
    retry_after_seconds as parse_retry_after_seconds,
)
from utils.request_execution import (
    wait_before_retry as wait_request_before_retry,
)

logger = logging.getLogger(__name__)

_SessionResult = TypeVar("_SessionResult")


class AuthTransitionKind(StrEnum):
    """Phases of one hidden authentication transaction."""

    ATTEMPT = "attempt"
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class AuthTransition:
    """Typed, paired lifecycle notification for hidden authentication."""

    transaction_id: int
    kind: AuthTransitionKind
    error: BaseException | None = None


@dataclass(frozen=True)
class _ClientSessionState:
    """Deep-copied APIClient state that must commit or roll back together."""

    headers: dict[str, Any]
    account_info: dict[str, Any]
    version_info: dict[str, Any]
    user_info: dict[str, Any]
    master_split_paths: list[str]
    master_split_paths_version_identity: dict[str, Any] | None
    pending_game_user_id: int | None


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
        self._master_split_paths_version_identity: dict[str, Any] | None = None
        self._pending_game_user_id: int | None = None

        self.logger = logger
        self.lifecycle_callback: Callable[[AuthTransition], None] | None = None
        self._auth_transaction_id = 0
        self.region = region
        self.headers = deepcopy(initial_api_headers[region])
        self.protocol = GameProtocolTransport(region, self.headers, logger)
        self.rate_limited = False
        self._recovering_426 = False
        self._authenticating = False

    def _capture_session_state(self) -> _ClientSessionState:
        """Capture mutable client state at an authentication boundary.

        The version document is captured alongside master split paths because
        its app version identifies the master metadata currently in use.
        """
        pending_game_user_id = getattr(self, "_pending_game_user_id", None)
        if not isinstance(pending_game_user_id, int) or isinstance(
            pending_game_user_id, bool
        ):
            pending_game_user_id = None
        split_paths_identity = getattr(
            self, "_master_split_paths_version_identity", None
        )
        if not isinstance(split_paths_identity, dict):
            split_paths_identity = None
        return _ClientSessionState(
            headers=deepcopy(self.headers),
            account_info=deepcopy(self.account_info),
            version_info=deepcopy(self.version_info),
            user_info=deepcopy(self.user_info),
            master_split_paths=deepcopy(self.master_split_paths),
            master_split_paths_version_identity=deepcopy(split_paths_identity),
            pending_game_user_id=pending_game_user_id,
        )

    def _restore_session_state(self, state: _ClientSessionState) -> None:
        """Restore a captured session without detaching the protocol headers."""
        self.headers.clear()
        self.headers.update(deepcopy(state.headers))
        self.account_info = deepcopy(state.account_info)
        self.version_info = deepcopy(state.version_info)
        self.user_info = deepcopy(state.user_info)
        self.master_split_paths = deepcopy(state.master_split_paths)
        self._master_split_paths_version_identity = deepcopy(
            state.master_split_paths_version_identity
        )
        self._pending_game_user_id = state.pending_game_user_id

    def _run_session_transaction(
        self, operation: Callable[[], _SessionResult]
    ) -> _SessionResult:
        """Commit an operation on success, restoring session state on failure."""
        previous_state = self._capture_session_state()
        try:
            return operation()
        except BaseException:
            self._restore_session_state(previous_state)
            raise

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
        self._master_split_paths_version_identity = None

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

    @staticmethod
    def _is_auth_endpoint(endpoint: str) -> bool:
        """Return True if *endpoint* is an authentication endpoint."""
        path = endpoint.split("?", 1)[0]
        return path == "/user/auth" or (
            path.startswith("/user/")
            and path.endswith("/auth")
            and path.count("/") == 3
        )

    def _update_version_after_426(self, *, endpoint: str | None = None) -> None:
        if self.region in ["jp"]:
            self._refresh_suite_version_headers()
        elif self.region in ["en"]:
            self._refresh_suite_version_headers()
        else:
            if not self._refresh_tw_kr_app_identity():
                ver_text = get_app_ver_qooapp(app_id_regions[self.region])
                self.headers["x-app-version"] = ver_text
            # TW/KR: ``check_versions`` is intentionally a no-op for these
            # regions, so the data/asset headers would otherwise stay stale
            # after a 426. Refresh them explicitly from the public system
            # endpoint so the retried request carries a complete version set.
            self._refresh_tw_kr_asset_data_versions()
        self.check_versions()
        if self.account_info and not self._is_auth_endpoint(endpoint or ""):
            self.login()

    def _refresh_tw_kr_app_identity(self) -> bool:
        """Apply the published TW/KR app version and hash.

        Returns ``False`` when the feed is unavailable; the current headers,
        seeded from ``APP_VER``/``APP_HASH``, then stay in place.
        """
        identity = get_app_identity(self.region)
        if identity is None:
            return False
        self.headers["x-app-version"] = identity["appVersion"]
        self.headers["x-app-hash"] = identity["appHash"]
        self.logger.info(
            "applied published app identity region=%s app_version=%s",
            self.region,
            identity["appVersion"],
        )
        return True

    def _refresh_tw_kr_asset_data_versions(self) -> None:
        """Refresh ``x-data-version``/``x-asset-version`` for TW/KR after 426.

        ``check_versions`` is a no-op for these regions, so derive the current
        data/asset versions directly from the public ``/system`` endpoint. The
        probe uses a Never retry policy so that a 426 on the probe cannot
        re-enter this recovery path (which would recurse or trigger an
        unintended login). Failures are non-fatal: the app version has already
        been refreshed and the original request is retried regardless.
        """
        if getattr(self, "_refreshing_426_system", False):
            # Re-entered while probing system data during 426 recovery; avoid
            # recursing into another system probe.
            return
        self._refreshing_426_system = True
        try:
            try:
                system_data = self.call_pjsk_api(
                    "/system",
                    "get",
                    retry_policy=RetryPolicy.NEVER,
                    bypass_error_recovery=True,
                )
                system_data = validate_system_data(system_data)
            except Exception as error:  # noqa: BLE001 - best-effort refresh
                self.logger.warning(
                    "TW/KR system data fetch failed during 426 recovery: %s", error
                )
                return

            all_ver_infos = system_data.get("appVersions") or []
            if not all_ver_infos:
                return
            curr_app_ver = self.headers["x-app-version"]
            try:
                curr_ver_info, fallback_selected = self._find_current_version_info(
                    all_ver_infos, curr_app_ver
                )
            except Exception as error:  # noqa: BLE001 - best-effort refresh
                self.logger.warning(
                    "TW/KR version lookup failed during 426 recovery: %s", error
                )
                return

            if "dataVersion" in curr_ver_info:
                self.headers["x-data-version"] = curr_ver_info["dataVersion"]
                self.version_info["dataVersion"] = curr_ver_info["dataVersion"]
            self.headers["x-asset-version"] = curr_ver_info["assetVersion"]
            self.version_info["assetVersion"] = curr_ver_info["assetVersion"]
            if fallback_selected:
                # The QooApp version was unavailable (or in maintenance), so the
                # probe fell back to a different available version. Synchronize
                # the app version fields so the retried request carries a
                # coherent version set rather than a stale app version.
                self.headers["x-app-version"] = curr_ver_info["appVersion"]
                self.version_info["appVersion"] = curr_ver_info["appVersion"]
        finally:
            self._refreshing_426_system = False

    def _handle_http_error_retry(  # noqa: C901 - established retry decision table
        self,
        response: requests.Response | None,
        res_data: Any,
        *,
        endpoint: str | None = None,
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
            if self._recovering_426:
                self.logger.warning(
                    "%s nested 426 during version recovery; aborting nested recovery",
                    self.region,
                )
                return False
            self.logger.warning("%s server should update version info", self.region)
            transaction_id: int | None = None
            if self.account_info:
                transaction_id = self._begin_auth_transition()
            self._recovering_426 = True
            try:
                self._run_session_transaction(
                    lambda: self._update_version_after_426(endpoint=endpoint)
                )
            except BaseException as error:
                if transaction_id is not None:
                    self._finish_auth_transition(transaction_id, error)
                raise
            finally:
                self._recovering_426 = False
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

            def accept_and_reauthenticate() -> None:
                self.accept_agreement()
                if self.account_info:
                    self.login()

            try:
                self._run_session_transaction(accept_and_reauthenticate)
            except BaseException as error:
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
            endpoint_path = (endpoint or "").split("?", 1)[0]
            endpoint_kind = (
                "suite_user"
                if endpoint_path.startswith("/suite/user/")
                else "auth"
                if self._is_auth_endpoint(endpoint_path)
                else "other"
            )
            self.logger.warning(
                "auth-related failure region=%s endpoint_kind=%s status=403 "
                "error_code=session_error",
                self.region,
                endpoint_kind,
            )
            if self._authenticating:
                self.logger.warning(
                    "authentication session rejected during active login; "
                    "skipping recursive login region=%s",
                    self.region,
                )
                return False
            transaction_id = None
            if self.account_info:
                transaction_id = self._begin_auth_transition()
            try:
                if self.account_info:
                    self._run_session_transaction(self.login)
            except BaseException as error:
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

    @staticmethod
    def _auth_version_identity(version_info: dict[str, Any]) -> dict[str, Any]:
        """Return the version fields that bind auth-provided split paths."""
        return {
            key: deepcopy(version_info[key])
            for key in ("appVersion", "dataVersion", "assetVersion", "cdnVersion")
            if key in version_info
        }

    def _split_path_context_digest(self) -> str | None:
        identity = self._master_split_paths_version_identity
        if identity is None:
            return None
        context = {
            "master_split_paths": list(self.master_split_paths),
            "version_identity": identity,
            "version_headers": {
                header: self.headers.get(header)
                for header in (
                    "x-app-version",
                    "x-data-version",
                    "x-asset-version",
                    "x-app-hash",
                )
            },
        }
        encoded = json.dumps(
            context, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _candidate_for_version_identity(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        """Complete optional candidate fields from matching authenticated state."""
        result = deepcopy(candidate)
        current = self.version_info
        if result.get("appVersion") == current.get("appVersion"):
            for key in ("dataVersion", "appHash", "assetHash", "multiPlayVersion"):
                if key not in result and key in current:
                    result[key] = deepcopy(current[key])
            if (
                (not isinstance(result.get("appHash"), str) or not result["appHash"])
                and isinstance(current.get("appHash"), str)
                and current["appHash"]
            ):
                result["appHash"] = current["appHash"]
        return result

    def _candidate_matches_split_path_identity(self, candidate: dict[str, Any]) -> bool:
        identity = self._master_split_paths_version_identity
        if identity is None or not self.master_split_paths:
            return False
        candidate_identity = self._auth_version_identity(candidate)
        return bool(identity) and all(
            candidate_identity.get(key) == value for key, value in identity.items()
        )

    def update_snapshot(self) -> dict[str, Any]:
        """Capture one candidate and its matching auth-derived split context.

        This method is intended to run as one serialized shared-client job. It
        performs at most one system discovery and only refreshes JP/EN auth when
        the installed split context does not identify that candidate.
        """
        discovery = self._discover_version_candidate()
        candidate = discovery["candidate_version_info"]
        snapshot: dict[str, Any] = {
            "maintenance": discovery["maintenance"],
            "candidate_version_info": deepcopy(candidate),
            "master_split_paths": deepcopy(self.master_split_paths)
            if self.region in ("jp", "en")
            else [],
            "split_path_version_identity": deepcopy(
                self._master_split_paths_version_identity
            ),
            "split_path_context_digest": self._split_path_context_digest(),
        }
        if discovery["maintenance"] or self.region not in ("jp", "en"):
            return snapshot

        candidate = self._candidate_for_version_identity(candidate)
        if not self._candidate_matches_split_path_identity(candidate):
            if not self.account_info:
                raise RuntimeError(
                    "Cannot refresh master split context without an account"
                )
            self.refresh_master_split_paths()
            candidate = self._candidate_for_version_identity(candidate)
            if not self._candidate_matches_split_path_identity(candidate):
                raise RuntimeError(
                    "Master split paths do not match the discovered version"
                )

        snapshot["candidate_version_info"] = deepcopy(candidate)
        snapshot["master_split_paths"] = deepcopy(self.master_split_paths)
        snapshot["split_path_version_identity"] = deepcopy(
            self._master_split_paths_version_identity
        )
        snapshot["split_path_context_digest"] = self._split_path_context_digest()
        return snapshot

    def _apply_new_version_info(self, curr_ver_info: dict[str, Any]) -> None:
        if "dataVersion" in curr_ver_info:
            self.headers["x-data-version"] = curr_ver_info["dataVersion"]
        self.headers["x-asset-version"] = curr_ver_info["assetVersion"]
        self.headers["x-app-version"] = curr_ver_info["appVersion"]
        new_app_hash = curr_ver_info.get("appHash")
        if (
            self.headers.get("x-app-hash", None) is not None
            and isinstance(new_app_hash, str)
            and new_app_hash != ""
        ):
            self.headers["x-app-hash"] = new_app_hash
        elif self.headers.get("x-app-hash", None) is None and self.region == "jp":
            ver_data = get_app_ver_and_hash_jp()
            self.headers["x-app-version"] = ver_data["appVersion"]
            self.headers["x-app-hash"] = ver_data["appHash"]
            self.version_info["appHash"] = ver_data["appHash"]
        elif self.headers.get("x-app-hash", None) is None and self.region == "en":
            ver_data = get_app_ver_and_hash_en()
            self.headers["x-app-version"] = ver_data["appVersion"]
            self.headers["x-app-hash"] = ver_data["appHash"]
            self.version_info["appHash"] = ver_data["appHash"]

    def _authenticate(self) -> dict[str, Any]:
        self._pending_game_user_id = None
        self.headers.pop("x-session-token", None)
        credential_type = "jp_en" if self.region in ("jp", "en") else "other"
        self.logger.info(
            "authentication request started region=%s credential_type=%s",
            self.region,
            credential_type,
        )
        credential: AccountCredential
        if self.region in ("jp", "en"):
            self._refresh_suite_version_headers()
            credential = self._build_jp_en_credential()
            self._apply_jp_en_fingerprint_headers(credential)
        elif self.region in ("tw", "kr"):
            credential = self._validate_tw_kr_account_info()
            self._refresh_tw_kr_app_identity()
        elif self.region == "cn":
            access_token = self.account_info["loginInfo"]["accessToken"]
            raw = self.call_pjsk_api(
                "/user/auth", "post", {"userID": 0, "accessToken": access_token}
            )
            # CN login response must carry a cdnVersion consumed by
            # ``_apply_auth_headers_and_version_info``.
            try:
                return validate_auth_response(raw, require_cdn_version=True)
            except ResponseValidationError as error:
                raise RuntimeError(f"Invalid login response: {error}") from error
        else:
            raise ValueError(f"Unsupported region: {self.region}")

        result = GameAuthenticationService(self).authenticate(credential)
        auth_data = result.data
        self.logger.info(
            "authentication response accepted region=%s credential_type=%s",
            self.region,
            credential_type,
        )
        # TW/KR (non-suite) login responses must also carry a cdnVersion of the
        # correct type; the region-agnostic auth service only validates the common
        # fields, so re-run validation requiring cdnVersion (rejecting bool/float
        # and a missing value) for consistency with the CN path.
        if self.region in ("tw", "kr"):
            try:
                validate_auth_response(auth_data, require_cdn_version=True)
            except ResponseValidationError as error:
                raise RuntimeError(f"Invalid login response: {error}") from error
        # Record split paths only after the region-specific validation above has
        # succeeded, so a malformed TW/KR auth response cannot leave stale/partial
        # split paths on the client.
        self.master_split_paths = list(result.master_split_paths)
        self._master_split_paths_version_identity = self._auth_version_identity(
            auth_data
        )
        if self.region in ("tw", "kr"):
            self._pending_game_user_id = result.canonical_user_id
        return auth_data

    def _build_jp_en_credential(self) -> JpEnCredential:
        def fingerprint_field(key: str) -> str:
            if key not in self.account_info:
                return ""
            value = self.account_info[key]
            if isinstance(value, str) and value:
                return value
            raise ValueError(f"JP/EN account info requires a non-empty {key}")

        return JpEnCredential(
            AccountRegion(self.region),
            str(self.account_info["userId"]),
            str(self.account_info["credential"]),
            str(self.account_info["signature"]),
            install_id=fingerprint_field("installId"),
            x_if=fingerprint_field("xIf"),
            x_kc=fingerprint_field("xKc"),
            device_model=fingerprint_field("deviceModel"),
            os_version=fingerprint_field("osVersion"),
            user_agent=fingerprint_field("userAgent"),
        )

    def _apply_jp_en_fingerprint_headers(self, credential: JpEnCredential) -> None:
        """Present the lease's registered device identity for JP/EN requests.

        Credentials without a fingerprint (legacy local accounts) are reset to
        the static per-region bootstrap headers so a previous lease's device
        identity never outlives its credential; remotely provisioned accounts
        are bound to the identity recorded at their registration.
        """
        if credential.has_device_fingerprint:
            install_id = credential.install_id
            x_if = credential.x_if
            x_kc = credential.x_kc
            device_model = credential.device_model
            os_version = credential.os_version
            user_agent = credential.user_agent
            source = "lease"
        else:
            static = initial_api_headers[self.region]
            install_id = static["x-install-id"]
            x_if = static["x-if"]
            x_kc = static["x-kc"]
            device_model = static["x-devicemodel"]
            os_version = static["x-operatingsystem"]
            user_agent = static["user-agent"]
            source = "static"
        self.headers["x-install-id"] = install_id
        self.headers["x-if"] = x_if
        self.headers["x-kc"] = x_kc
        self.headers["x-devicemodel"] = device_model
        self.headers["x-operatingsystem"] = os_version
        self.headers["user-agent"] = user_agent
        self.logger.info(
            "applied %s device fingerprint region=%s",
            source,
            self.region,
        )

    def _refresh_suite_version_headers(self) -> None:
        """Use the current suite client fingerprint before authenticating."""
        fetch_version = get_app_ver_and_hash_jp
        fallback_version = JP_FALLBACK_VERSION_INFO
        if self.region == "en":
            fetch_version = get_app_ver_and_hash_en
            fallback_version = EN_FALLBACK_VERSION_INFO

        try:
            version = validate_version_info(fetch_version(), require_app_hash=True)
        except Exception as error:  # noqa: BLE001 - retain a valid local fingerprint
            logger.warning(
                "suite version refresh before authentication failed region=%s; "
                "using local fallback error_type=%s",
                self.region,
                type(error).__name__,
            )
            version = validate_version_info(fallback_version, require_app_hash=True)

        try:
            for source_key, header_key in (
                ("appVersion", "x-app-version"),
                ("dataVersion", "x-data-version"),
                ("assetVersion", "x-asset-version"),
                ("appHash", "x-app-hash"),
            ):
                value = version.get(source_key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    self.headers[header_key] = str(value)
            self.logger.info(
                "refreshed suite version headers before authentication region=%s "
                "app_version=%s",
                self.region,
                self.headers.get("x-app-version"),
            )
        except Exception as error:  # noqa: BLE001 - preserve refresh error handling
            self.logger.warning(
                "suite version header application failed region=%s error_type=%s",
                self.region,
                type(error).__name__,
            )

    def _validate_tw_kr_account_info(self) -> TwKrCredential:
        required_keys = (
            "deviceId",
            "installId",
            "userAgent",
            "deviceModel",
            "osVersion",
        )
        missing = [key for key in required_keys if key not in self.account_info]
        if missing:
            raise ValueError(f"TW/KR account info missing keys: {', '.join(missing)}")
        device_id = self.account_info["deviceId"]
        install_id = self.account_info["installId"]
        user_agent = self.account_info["userAgent"]
        device_model = self.account_info["deviceModel"]
        os_version = self.account_info["osVersion"]
        if self.region == "tw":
            os_header = "x-operatingSystem"
        else:
            os_header = "x-operatingsystem"
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("TW/KR account info requires a non-empty deviceId")
        if not isinstance(install_id, str) or not install_id:
            raise ValueError("TW/KR account info requires a non-empty installId")
        if not isinstance(user_agent, str) or not user_agent:
            raise ValueError("TW/KR account info requires a non-empty userAgent")
        if not isinstance(device_model, str) or not device_model:
            raise ValueError("TW/KR account info requires a non-empty deviceModel")
        if not isinstance(os_version, str) or not os_version:
            raise ValueError("TW/KR account info requires a non-empty osVersion")
        # Optional service-provided header values; absent or empty keeps the
        # static x-platform and the bare osVersion header.
        platform = self._optional_tw_kr_header_value("platform")
        operating_system = self._optional_tw_kr_header_value("operatingSystem")
        self.headers["device_id"] = device_id
        self.headers["x-install-id"] = install_id
        self.headers["user-agent"] = user_agent
        self.headers["x-devicemodel"] = device_model
        self.headers[os_header] = operating_system or os_version
        if platform:
            self.headers["x-platform"] = platform
        return TwKrCredential(
            AccountRegion(self.region),
            str(self.account_info["userId"]),
            str(self.account_info["loginInfo"]["accessToken"]),
            device_id,
            install_id,
            user_agent,
            device_model,
            os_version,
            platform=platform,
            operating_system=operating_system,
        )

    def _optional_tw_kr_header_value(self, key: str) -> str:
        value = self.account_info.get(key) or ""
        if not isinstance(value, str):
            raise ValueError(f"TW/KR account info {key} must be a string")
        return value

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
                "appVersionStatus": (
                    auth_data["appVersionStatus"]
                    if self.region in ("tw", "kr")
                    else "available"
                ),
                "cdnVersion": auth_data["cdnVersion"],
            }
            return

        self.version_info["appVersion"] = app_ver
        self.version_info["assetVersion"] = asset_ver
        self.version_info["dataVersion"] = data_ver
        # Keep the version document complete. JP/EN auth responses do not carry
        # appHash, but the request headers already contain the validated value.
        app_hash = self.headers.get("x-app-hash")
        if isinstance(app_hash, str) and app_hash != "":
            self.version_info["appHash"] = app_hash
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

    def _user_id_for_api(self) -> str:
        """Return the authenticated game ID, normalized at the API boundary."""
        user_id = self._pending_game_user_id
        if user_id is None:
            user_id = self.account_info["userId"]
        return str(user_id)

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
        *,
        bypass_error_recovery: bool = False,
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
            bypass_error_recovery: When True, HTTP error responses are not
                routed through ``_handle_http_error_retry`` (e.g. cookie refresh,
                the 426 version recovery that can trigger a re-login). Used by
                internal probes that must not recurse into recovery side effects.

        Returns:
            Decrypted response data (bytes or dict)

        Raises:
            RuntimeError: If API call fails after all retries
            ValueError: If body type is not str or dict
        """
        if self.rate_limited:
            raise RuntimeError("Cooling down for rate limit...")

        def recover_http_error(
            response: requests.Response | None, response_data: Any
        ) -> bool:
            return self._handle_http_error_retry(
                response, response_data, endpoint=endpoint
            )

        recovery_handler = None if bypass_error_recovery else recover_http_error
        return execute_request(
            endpoint,
            method,
            body,
            retry_policy,
            max_retries=Config.MAX_API_RETRIES,
            logger=self.logger,
            encrypt_request_body=self._encrypt_request_body,
            send_request=self._send_api_request,
            decrypt_response=self._decrypt_response_data,
            recovery_handler=recovery_handler,
            wait_for_retry=self._wait_before_retry,
        )

    @staticmethod
    def _retry_after_seconds(response: requests.Response | None) -> float | None:
        return parse_retry_after_seconds(response, now=lambda: datetime.now(UTC))

    def _wait_before_retry(
        self, response: requests.Response | None, attempt: int
    ) -> None:
        wait_request_before_retry(
            response,
            attempt,
            get_retry_after=self._retry_after_seconds,
            set_rate_limited=lambda value: setattr(self, "rate_limited", value),
            sleep_fn=sleep,
            jitter_fn=random.uniform,
        )

    def _discover_version_candidate(self) -> dict[str, Any]:
        """Read a candidate without applying it to client state on success.

        A successful ``/system`` response is only inspected here; version
        headers and ``version_info`` are not updated. An HTTP 426 still follows
        ``call_pjsk_api``'s established recovery path before this method receives
        a response. That recovery may intentionally refresh version/auth state
        (including a login for the non-auth ``/system`` endpoint); discovery
        neither bypasses nor changes those semantics.
        """
        if self.region not in ("jp", "en"):
            candidate = deepcopy(self.version_info)
            return {
                "maintenance": candidate.get("appVersionStatus") == "maintenance",
                "new_version": False,
                "candidate_version_info": candidate,
                "current_version_info": candidate,
                "fallback_selected": False,
            }

        system_data = self.fetch_system_data()
        maintenance = system_data.get("maintenanceStatus") == "maintenance_in"
        curr_ver_info, fallback_selected = self._find_current_version_info(
            system_data["appVersions"], self.headers["x-app-version"]
        )
        if curr_ver_info["appVersionStatus"] == "maintenance" and fallback_selected:
            maintenance = True
            new_version = False
        elif fallback_selected:
            new_version = True
        else:
            new_version = self._is_version_updated(curr_ver_info)

        return {
            "maintenance": maintenance,
            "new_version": new_version,
            "candidate_version_info": self._candidate_for_version_identity(
                curr_ver_info
            ),
            "current_version_info": curr_ver_info,
            "fallback_selected": fallback_selected,
        }

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
            Maintenance and version-change flags plus the current version info
            snapshot after any update has been applied.
        """
        res: dict[str, Any] = {
            "maintenance": False,
            "new_version": False,
            "version_info": deepcopy(self.version_info),
        }

        if self.region in ("cn", "tw", "kr"):
            return res

        discovery = self._discover_version_candidate()
        res["maintenance"] = discovery["maintenance"]
        res["new_version"] = discovery["new_version"]
        curr_ver_info = discovery["current_version_info"]

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
            value = curr_ver_info[key]
            # Preserve an already-valid appHash. Upstream system data for JP/EN
            # may omit or empty ``appHash``; do not let that clobber the
            # validated hash carried in the request headers.
            if (
                key == "appHash"
                and (not isinstance(value, str) or value == "")
                and isinstance(self.version_info.get("appHash"), str)
                and (self.version_info.get("appHash") != "")
            ):
                continue
            self.version_info[key] = value

        if input_ver_info:
            res["new_version"] = (
                (
                    "dataVersion" in input_ver_info
                    and "dataVersion" in curr_ver_info
                    and input_ver_info["dataVersion"] != curr_ver_info["dataVersion"]
                )
                or input_ver_info["assetVersion"] != curr_ver_info["assetVersion"]
                or input_ver_info["appVersion"] != curr_ver_info["appVersion"]
            )

        res["version_info"] = deepcopy(self.version_info)
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
        if self._authenticating:
            raise RuntimeError("authentication already in progress")
        self._authenticating = True
        try:
            return self._run_session_transaction(self._login)
        finally:
            self._authenticating = False

    def _login(self) -> dict[str, Any]:
        """Perform login work inside ``login``'s session transaction."""
        self._pending_game_user_id = None
        self.logger.info("simulate login process")
        self.logger.debug("do auth")
        auth_data = self._authenticate()
        self._apply_auth_headers_and_version_info(auth_data)

        self.logger.debug("get suite user")
        user_id = self._user_id_for_api()
        user_info = self.fetch_suite_user()

        self.logger.debug("check and skip tutorial")
        self._complete_tutorial_if_needed(user_id, user_info)
        self._post_login_refresh(user_id)

        if self._pending_game_user_id is not None:
            self.account_info["userId"] = str(self._pending_game_user_id)
        self.user_info = user_info
        self._pending_game_user_id = None
        return user_info

    def refresh_master_split_paths(self) -> list[str]:
        """Refresh authentication metadata without running post-login user requests."""
        if self.region not in ("jp", "en"):
            raise ValueError("Split master paths are only available for jp and en")

        def refresh() -> list[str]:
            auth_data = self._authenticate()
            self._apply_auth_headers_and_version_info(auth_data)
            return self.master_split_paths

        return self._run_session_transaction(refresh)

    def fetch_suite_user(self, update_user_info: bool = False) -> dict[str, Any]:
        res = GameAPIService(self, self._user_id_for_api()).fetch_suite_user()

        if update_user_info:
            self.user_info = res

        return res

    def fetch_user_profile(self, user_id: str) -> dict[str, Any]:
        return GameAPIService(self, self._user_id_for_api()).fetch_user_profile(user_id)

    def fetch_user_event_ranking(
        self, target_user_id: str, event_id: int
    ) -> dict[str, Any]:
        # cn/tw/kr ranking requests have no target-user mode; the server
        # rejects ``targetUserId``, so fail locally instead of calling it.
        if self.region in ("cn", "tw", "kr"):
            raise ValueError(
                f"target user event ranking is not supported for region {self.region}"
            )
        return GameAPIService(self, self._user_id_for_api()).fetch_user_event_ranking(
            target_user_id, event_id
        )

    def fetch_information(self):
        return PublicGameAPIService(self).fetch_information()

    def fetch_system_data(self) -> dict[str, Any]:
        return PublicGameAPIService(self).fetch_system_data()

    def fetch_event_rank_first_100(self, event_id: int) -> dict[str, Any]:
        return GameAPIService(self, self._user_id_for_api()).fetch_event_rank_first_100(
            event_id
        )

    def fetch_event_rank_border(self, event_id: int) -> dict[str, Any]:
        # cn/tw/kr only serve the user-scoped ranking-border path.
        if self.region in ("cn", "tw", "kr"):
            return GameAPIService(
                self, self._user_id_for_api()
            ).fetch_event_rank_border(event_id)
        return PublicGameAPIService(self).fetch_event_rank_border(event_id)

    def accept_agreement(self):
        return GameAPIService(self, self._user_id_for_api()).accept_agreement(
            str(self.account_info["credential"]),
        )

    def fetch_master_split(
        self,
        split_path: str,
        expected_split_path_digest: str | None = None,
    ) -> Any:
        """Fetch a single master-data split by path (GET only).

        Only allowlisted split paths (present in ``master_split_paths``) are
        permitted. This is the safe, scoped replacement for the generic
        ``call_pjsk_api("/<split>")`` passthrough. An optional digest binds the
        fetch to the snapshot used by a caller; one re-authentication is allowed
        to repair stale local context, but a second mismatch fails closed.
        """
        if expected_split_path_digest is not None:
            if len(expected_split_path_digest) != 64 or any(
                char not in "0123456789abcdef" for char in expected_split_path_digest
            ):
                raise ValueError("Invalid expected master split context digest")
            if self._split_path_context_digest() != expected_split_path_digest:
                if self.region not in ("jp", "en") or not self.account_info:
                    raise RuntimeError("Master split context is stale")
                self.refresh_master_split_paths()
                if self._split_path_context_digest() != expected_split_path_digest:
                    raise RuntimeError("Master split context remains stale after auth")

        if split_path not in self.master_split_paths:
            raise ValueError(
                f"Master split path {split_path!r} is not in the allowlist"
            )
        result = self.call_pjsk_api(f"/{split_path}")
        if (
            expected_split_path_digest is not None
            and self._split_path_context_digest() != expected_split_path_digest
        ):
            raise RuntimeError("Master split context changed while fetching")
        return result

    def request_and_decrypt(
        self,
        url: str,
        method: str = "get",
        body: str | dict[str, Any] = "",
    ) -> Any:
        self._validate_request_and_decrypt_url(url, method, body)
        self.logger.debug(
            "request_and_decrypt method=%s url=%s body=%s",
            method,
            url,
            body,
        )
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

        decrypted = decrypt_msgpack(res.content)
        self.logger.debug(
            "request_and_decrypt status=%s decrypted_type=%s decrypted_size=%s",
            res.status_code,
            type(decrypted).__name__,
            len(decrypted) if hasattr(decrypted, "__len__") else "N/A",
        )
        return decrypted

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
