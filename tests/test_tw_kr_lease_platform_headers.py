"""TW/KR platform and operating-system headers from account-service leases."""

from __future__ import annotations

from typing import Any

import pytest

from accounts.local import credential_to_account_info
from accounts.models import AccountRegion, TwKrCredential
from accounts.remote import RemoteAccountProvider
from api_client import APIClient

OPERATING_SYSTEM = "Android OS 14 / API-34"
OS_HEADERS = [("tw", "x-operatingSystem"), ("kr", "x-operatingsystem")]


def _payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "tw_kr",
        "sdk_open_id": "open-id",
        "access_token": "token",
        "device_id": "device-id",
        "install_id": "install-id",
        "user_agent": "user-agent",
        "device_model": "device-model",
        "os_version": "14",
    }
    payload.update(extra)
    return payload


def _credential(region: str, **extra: str) -> TwKrCredential:
    return TwKrCredential(
        AccountRegion(region),
        "open-id",
        "token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "14",
        **extra,
    )


def test_remote_credential_reads_platform_and_operating_system():
    credential = RemoteAccountProvider._credential(
        AccountRegion.KR,
        _payload(platform="Android", operating_system=OPERATING_SYSTEM),
    )

    assert credential.platform == "Android"
    assert credential.operating_system == OPERATING_SYSTEM


@pytest.mark.parametrize("extra", [{}, {"platform": None, "operating_system": None}])
def test_remote_credential_treats_missing_or_null_values_as_empty(extra):
    credential = RemoteAccountProvider._credential(AccountRegion.KR, _payload(**extra))

    assert credential.platform == ""
    assert credential.operating_system == ""


@pytest.mark.parametrize("key", ["platform", "operating_system"])
def test_remote_credential_rejects_non_string_values(key):
    with pytest.raises(ValueError):
        RemoteAccountProvider._credential(AccountRegion.KR, _payload(**{key: 14}))


def test_account_info_carries_values_only_when_present():
    with_values = credential_to_account_info(
        _credential("kr", platform="Android", operating_system=OPERATING_SYSTEM)
    )
    without_values = credential_to_account_info(_credential("kr"))

    assert with_values["platform"] == "Android"
    assert with_values["operatingSystem"] == OPERATING_SYSTEM
    assert "platform" not in without_values
    assert "operatingSystem" not in without_values


@pytest.mark.parametrize(("region", "os_header"), OS_HEADERS)
def test_auth_headers_use_service_platform_and_operating_system(region, os_header):
    client = APIClient(region=region)
    client.account_info = credential_to_account_info(
        _credential(region, platform="Android", operating_system=OPERATING_SYSTEM)
    )

    credential = client._validate_tw_kr_account_info()

    assert client.headers[os_header] == OPERATING_SYSTEM
    assert client.headers["x-platform"] == "Android"
    assert credential.platform == "Android"
    assert credential.operating_system == OPERATING_SYSTEM


@pytest.mark.parametrize(("region", "os_header"), OS_HEADERS)
def test_auth_headers_fall_back_without_service_values(region, os_header):
    client = APIClient(region=region)
    static_platform = client.headers["x-platform"]
    client.account_info = credential_to_account_info(_credential(region))

    client._validate_tw_kr_account_info()

    assert client.headers[os_header] == "14"
    assert client.headers["x-platform"] == static_platform


@pytest.mark.parametrize("key", ["platform", "operatingSystem"])
def test_auth_rejects_non_string_values_without_mutating_headers(key):
    client = APIClient(region="kr")
    original_headers = dict(client.headers)
    client.account_info = credential_to_account_info(_credential("kr"))
    client.account_info[key] = 14

    with pytest.raises(ValueError, match=f"{key} must be a string"):
        client._validate_tw_kr_account_info()

    assert client.headers == original_headers
