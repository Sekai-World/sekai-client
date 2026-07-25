"""
Centralized configuration management for sekai-client.

This module provides a single point for reading and validating environment
variables with sensible defaults. All environment parsing happens here,
enabling easy configuration reloading without process restart.
"""

import logging
from os import getenv

logger = logging.getLogger(__name__)


def _parse_float_env(name: str, default: float) -> float:
    """
    Parse a float environment variable with validation.

    Args:
        name: Environment variable name
        default: Default value if not set or invalid

    Returns:
        Parsed float value or default. Invalid or non-positive values
        are logged and replaced by the default.
    """
    raw = getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s value=%r, falling back to %.1f", name, raw, default)
        return default

    if value <= 0:
        logger.warning(
            "Invalid %s value=%r, falling back to %.1f because it must be positive",
            name,
            raw,
            default,
        )
        return default

    return value


def _parse_int_env(name: str, default: int) -> int:
    """
    Parse an int environment variable with validation.

    Args:
        name: Environment variable name
        default: Default value if not set or invalid

    Returns:
        Parsed int value or default. Negative values are invalid and
        fall back to the default; zero is allowed.
    """
    raw = getenv(name, str(default))
    try:
        value = int(raw)
        if value < 0:
            raise ValueError("value must be non-negative")
        return value
    except (TypeError, ValueError):
        logger.warning("Invalid %s value=%r, falling back to %d", name, raw, default)
        return default


def _parse_port_env(name: str, default: int) -> int:
    """Parse a configured TCP port using the explicit valid port range.

    Invalid numeric ports are retained so ``validate_region_config`` can reject
    them at startup.  Non-numeric values retain the ordinary integer parser's
    fallback behavior.
    """
    raw = getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s value=%r, falling back to %d", name, raw, default)
        return default

    if not 1 <= value <= 65535:
        logger.warning(
            "Invalid %s value=%r; ports must be between 1 and 65535",
            name,
            raw,
        )
    return value


def _is_valid_port(value: object) -> bool:
    """Return whether a configured value is a valid TCP port."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
    )


def _parse_str_env(name: str, default: str = "") -> str:
    """
    Parse a string environment variable.

    Args:
        name: Environment variable name
        default: Default value if not set

    Returns:
        Environment variable value or default
    """
    return getenv(name, default)


class Config:
    """
    Configuration container for sekai-client.

    Most settings are parsed at module load time with proper validation.
    API_TOKEN is read dynamically through get_api_token().
    """

    # ============ Request & Timeout Configuration ============
    REQUEST_TIMEOUT: float = _parse_float_env("REQUEST_TIMEOUT", 150.0)
    """Timeout for all external HTTP requests (in seconds)"""

    JOB_QUEUE_TIMEOUT: float = _parse_float_env("JOB_QUEUE_TIMEOUT", 30.0)
    """Timeout for enqueuing jobs to the worker (in seconds)"""

    ANSWER_QUEUE_TIMEOUT: float = _parse_float_env("ANSWER_QUEUE_TIMEOUT", 180.0)
    """Timeout for waiting for worker response (in seconds)"""

    WORKER_RESPONSE_TIMEOUT: float = 1.0
    """Timeout for worker to put response in queue (in seconds)"""

    # ============ Retry Configuration ============
    MAX_API_RETRIES: int = _parse_int_env("MAX_API_RETRIES", 3)
    """Maximum number of retries for API calls"""

    BOOTSTRAP_MAX_RETRIES: int = _parse_int_env("BOOTSTRAP_MAX_RETRIES", 3)
    """Maximum number of bootstrap retries"""

    LIFECYCLE_RETRY_BASE_SECONDS: float = _parse_float_env(
        "LIFECYCLE_RETRY_BASE_SECONDS", 1.0
    )
    """Initial delay between failed shared-client lifecycle attempts."""

    LIFECYCLE_RETRY_MAX_SECONDS: float = _parse_float_env(
        "LIFECYCLE_RETRY_MAX_SECONDS", 60.0
    )
    """Maximum delay between failed shared-client lifecycle attempts."""

    # ============ Region Port Configuration ============
    # Formal service regions. CN is NOT a formally deployed region; only a
    # standalone simplified checkUpdate-cn process is kept (see D-001 in
    # docs/remediation-roadmap.md). Keep CN_PORT/port-map for that process.
    REGIONS: list[str] = ["jp", "en", "tw", "kr"]
    """List of formally supported game regions (CN is excluded, see D-001)"""

    JP_PORT: int = _parse_port_env("JP_PORT", 39390)
    """Port for Japan region JSON-RPC server"""

    EN_PORT: int = _parse_port_env("EN_PORT", 39392)
    """Port for English region JSON-RPC server"""

    CN_PORT: int = _parse_port_env("CN_PORT", 39394)
    """Port for China region JSON-RPC server"""

    TW_PORT: int = _parse_port_env("TW_PORT", 39391)
    """Port for Taiwan region JSON-RPC server"""

    KR_PORT: int = _parse_port_env("KR_PORT", 39393)
    """Port for Korea region JSON-RPC server"""

    @classmethod
    def get_region_port(cls, region: str) -> int:
        """
        Get the JSON-RPC server port for a specific region.

        Args:
            region: Region code ('jp', 'en', 'tw', 'kr', and 'cn' for the
                standalone simplified checkUpdate process, see D-001)

        Returns:
            Port number

        Raises:
            ValueError: If region is not supported
        """
        port_map = {
            "jp": cls.JP_PORT,
            "en": cls.EN_PORT,
            "cn": cls.CN_PORT,
            "tw": cls.TW_PORT,
            "kr": cls.KR_PORT,
        }
        if region not in port_map:
            raise ValueError(f"Unsupported region: {region}")
        return port_map[region]

    @classmethod
    def validate_region_config(cls) -> list[str]:
        """
        Validate that every formally supported region has complete mappings.

        Ensures each region in ``REGIONS`` is declared in the headers map,
        the API URL map, and the RPC port map. Missing mappings are returned
        as error strings so callers can fail fast at startup.

        CN is intentionally excluded from ``REGIONS`` and is not checked here;
        the simplified checkUpdate-cn process relies on its own config and is
        not a formal service region (see D-001).

        Returns:
            List of region-mapping errors (empty if all regions are complete)
        """
        from utils.constants import base_pjsk_api_url, initial_api_headers

        port_map = {
            "jp": cls.JP_PORT,
            "en": cls.EN_PORT,
            "cn": cls.CN_PORT,
            "tw": cls.TW_PORT,
            "kr": cls.KR_PORT,
        }

        errors: list[str] = []
        for region in cls.REGIONS:
            if region not in initial_api_headers:
                errors.append(f"Region {region!r} missing API headers mapping")
            if region not in base_pjsk_api_url:
                errors.append(f"Region {region!r} missing API URL mapping")
            if region not in port_map:
                errors.append(f"Region {region!r} missing RPC port mapping")

        for region, port in port_map.items():
            if not _is_valid_port(port):
                errors.append(
                    f"Region {region!r} RPC port must be between 1 and 65535; "
                    f"got {port!r}"
                )
        return errors

    # ============ API & Security Configuration ============
    @classmethod
    def get_api_token(cls) -> str:
        """Read API token from environment on each access."""
        return _parse_str_env("API_TOKEN", "")

    @classmethod
    def get_internal_rpc_token(cls) -> str:
        """Read the internal JSON-RPC auth token on each access.

        Used to authenticate intra-host RPC between shared_client / check_update
        / event_tracker / api_public_server. Must be set (or bypass disabled via
        ALLOW_INSECURE_INTERNAL_RPC on loopback) or requests fail-closed.
        """
        return _parse_str_env("INTERNAL_RPC_TOKEN", "")

    @classmethod
    def allow_insecure_internal_rpc(cls) -> bool:
        """Whether to allow unauthenticated RPC from loopback.

        Off by default (fail-closed). Only honored for requests originating
        from 127.0.0.1 / ::1; non-loopback callers are always rejected.
        """
        return _parse_str_env("ALLOW_INSECURE_INTERNAL_RPC", "") in (
            "true",
            "True",
            "1",
        )

    @classmethod
    def enable_unsafe_pjsk_rpc(cls) -> bool:
        """Whether to expose the generic ``call_pjsk_api`` RPC.

        Off by default. The generic passthrough is intentionally disabled because
        it forwards arbitrary endpoints/methods/body to the game server. Scoped
        helpers (e.g. ``fetch_master_split``) must be used instead.
        """
        return _parse_str_env("ENABLE_UNSAFE_PJSK_RPC", "") in (
            "true",
            "True",
            "1",
        )

    LOGLEVEL: str = _parse_str_env("LOGLEVEL", "INFO").upper()
    """Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"""

    # ============ Service Dashboard Configuration ============
    PM2_BIN: str = _parse_str_env("PM2_BIN", "pm2")
    """PM2 executable used by the service dashboard"""

    SERVICE_SHARED_CLIENT_TEMPLATE: str = _parse_str_env(
        "SERVICE_SHARED_CLIENT_TEMPLATE", "sharedApiClient-{region}"
    )
    """PM2 name template for shared_client services"""

    SERVICE_CHECK_UPDATE_TEMPLATE: str = _parse_str_env(
        "SERVICE_CHECK_UPDATE_TEMPLATE", "checkUpdate-{region}"
    )
    """PM2 name template for check_update services"""

    SERVICE_EVENT_TRACKER_TEMPLATE: str = _parse_str_env(
        "SERVICE_EVENT_TRACKER_TEMPLATE", "eventTracker-{region}"
    )
    """PM2 name template for event_tracker services"""

    SERVICE_LOG_TAIL_LINES: int = _parse_int_env("SERVICE_LOG_TAIL_LINES", 300)
    """Number of recent PM2 log lines scanned by the dashboard"""

    SERVICE_STABLE_WAIT_SECONDS: int = _parse_int_env("SERVICE_STABLE_WAIT_SECONDS", 8)
    """Seconds to wait after a PM2 restart before continuing a restart sequence"""

    @classmethod
    def validate(cls) -> list[str]:
        """
        Validate critical configuration values.

        Returns:
            List of validation warnings (empty if all valid)
        """
        warnings = []

        region_errors = cls.validate_region_config()
        warnings.extend(region_errors)

        if not cls.get_api_token():
            warnings.append("API_TOKEN not set; requests will return 500 (fail-closed)")

        if cls.REQUEST_TIMEOUT <= 0:
            warnings.append("REQUEST_TIMEOUT must be positive")

        if cls.JOB_QUEUE_TIMEOUT <= 0:
            warnings.append("JOB_QUEUE_TIMEOUT must be positive")

        if cls.ANSWER_QUEUE_TIMEOUT <= 0:
            warnings.append("ANSWER_QUEUE_TIMEOUT must be positive")

        return warnings
