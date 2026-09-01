"""Small, check-update-independent helpers for refreshing user information."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from response_models import ResponseValidationError, validate_information

logger = logging.getLogger(__name__)


def bootstrap_init_client(client: Any, region: str) -> None:
    """Initialize the JSON-RPC client for ``region`` or terminate the process."""
    if not client.request("is_init") and not client.request("init", [region]):
        sys.exit(1)
    logger.info("[bootstrap] PJSK client inited")


def save_info_from_suite_user(
    client: Any, region: str, write_master_file: Callable[[str, Any], None]
) -> Any:
    """Fetch and persist suite-user data, preserving regional behavior."""
    suite_user = client.request("login_user_info")

    logger.debug("[save_info_from_suite_user] write user home banners")
    write_master_file("userHomeBanners.json", suite_user["userHomeBanners"])

    if region == "en":
        refresh_information(client, write_master_file)
    elif suite_user.get("userInformations", None):
        logger.debug("[save_info_from_suite_user] write user informations")
        write_master_file("userInformations.json", suite_user["userInformations"])

    logger.debug("[save_info_from_suite_user] finished")
    return suite_user


def refresh_information(
    client: Any, write_master_file: Callable[[str, Any], None]
) -> None:
    """Fetch, validate, and persist the current user information response."""
    logger.debug("[refresh_information] get informations")
    res = client.request("fetch_information")
    try:
        res = validate_information(res)
    except ResponseValidationError as error:
        raise RuntimeError(f"Invalid information response: {error}") from error

    logger.debug("[refresh_information] write user informations")
    write_master_file("userInformations.json", res["informations"])
