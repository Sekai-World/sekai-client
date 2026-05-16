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
        Parsed float value or default

    Raises:
        ValueError: If value is not positive
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
        Parsed int value or default
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


class _DynamicStrConfig:
    def __init__(self, name: str, default: str = "") -> None:
        self.name = name
        self.default = default

    def __get__(self, instance: object, owner: type | None = None) -> str:
        return _parse_str_env(self.name, self.default)


class Config:
    """
    Configuration container for sekai-client.

    All settings are parsed at module load time with proper validation.
    Provides defaults for all parameters to ensure graceful degradation.
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

    # ============ Region Port Configuration ============
    REGIONS: list[str] = ["jp", "en", "cn", "tw", "kr"]
    """List of supported game regions"""

    JP_PORT: int = _parse_int_env("JP_PORT", 39390)
    """Port for Japan region JSON-RPC server"""

    EN_PORT: int = _parse_int_env("EN_PORT", 39392)
    """Port for English region JSON-RPC server"""

    CN_PORT: int = _parse_int_env("CN_PORT", 39394)
    """Port for China region JSON-RPC server"""

    TW_PORT: int = _parse_int_env("TW_PORT", 39391)
    """Port for Taiwan region JSON-RPC server"""

    KR_PORT: int = _parse_int_env("KR_PORT", 39393)
    """Port for Korea region JSON-RPC server"""

    @classmethod
    def get_region_port(cls, region: str) -> int:
        """
        Get the JSON-RPC server port for a specific region.

        Args:
            region: Region code ('jp', 'en', 'cn', 'tw', 'kr')

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

    # ============ API & Security Configuration ============
    API_TOKEN: str = _DynamicStrConfig("API_TOKEN", "")
    """API token for request authentication"""

    LOGLEVEL: str = _parse_str_env("LOGLEVEL", "INFO").upper()
    """Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"""

    @classmethod
    def validate(cls) -> list[str]:
        """
        Validate critical configuration values.

        Returns:
            List of validation warnings (empty if all valid)
        """
        warnings = []

        if not cls.API_TOKEN:
            warnings.append("API_TOKEN not set; requests will return 500 (fail-closed)")

        if cls.REQUEST_TIMEOUT <= 0:
            warnings.append("REQUEST_TIMEOUT must be positive")

        if cls.JOB_QUEUE_TIMEOUT <= 0:
            warnings.append("JOB_QUEUE_TIMEOUT must be positive")

        if cls.ANSWER_QUEUE_TIMEOUT <= 0:
            warnings.append("ANSWER_QUEUE_TIMEOUT must be positive")

        return warnings


# Log validation warnings at module load
_validation_warnings = Config.validate()
for warning in _validation_warnings:
    logger.warning("Config validation: %s", warning)
