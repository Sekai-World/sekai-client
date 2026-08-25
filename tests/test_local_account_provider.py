"""Tests for the compatibility account provider used by current deployments."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import yaml

from accounts import (
    AccountRegion,
    AccountUnavailableError,
    InvalidAccountReason,
    InvalidLeaseError,
    JpEnCredential,
)
from accounts.local import LocalAccountProvider, credential_to_account_info


def test_loads_existing_jp_yaml_and_restricts_permissions(tmp_path):
    account_path = tmp_path / "sharedAccount.jp.yaml"
    account_path.write_text("userId: '1'\ncredential: secret\nsignature: signed\n")
    account_path.chmod(0o644)
    provider = LocalAccountProvider(tmp_path)

    lease = provider.acquire(
        AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-1"
    )

    assert credential_to_account_info(lease.credential) == {
        "userId": "1",
        "credential": "secret",
        "signature": "signed",
    }
    assert account_path.stat().st_mode & 0o777 == 0o600


def test_registers_missing_account_atomically(tmp_path):
    register = Mock(
        return_value=JpEnCredential(
            AccountRegion.EN, "registered-user", "encoded", "signed"
        )
    )
    provider = LocalAccountProvider(tmp_path, register)

    lease = provider.acquire(
        AccountRegion.EN, "worker", ttl_seconds=60, idempotency_key="request-1"
    )

    account_path = tmp_path / "sharedAccount.en.yaml"
    assert credential_to_account_info(lease.credential)["userId"] == "registered-user"
    assert yaml.safe_load(account_path.read_text())["credential"] == "encoded"
    assert account_path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".sharedAccount.*.tmp")) == []


def test_atomic_registration_write_cleans_up_after_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "accounts.local.yaml.safe_dump",
        Mock(side_effect=OSError("disk full")),
    )
    provider = LocalAccountProvider(
        tmp_path,
        lambda region: JpEnCredential(region, "1", "encoded", "signed"),
    )

    with pytest.raises(OSError):
        provider.acquire(
            AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-1"
        )
    assert list(tmp_path.glob(".sharedAccount.*.tmp")) == []


@pytest.mark.parametrize("region", [AccountRegion.TW, AccountRegion.KR])
def test_loads_tw_kr_environment_credentials(monkeypatch, tmp_path, region):
    prefix = f"SEKAI_{region.value.upper()}"
    monkeypatch.setenv(f"{prefix}_ACCESS_TOKEN", "token")
    monkeypatch.setenv(f"{prefix}_SDK_OPEN_ID", "open-id")
    monkeypatch.setenv(f"{prefix}_DEVICE_ID", "device-id")
    monkeypatch.setenv(f"{prefix}_INSTALL_ID", "install-id")
    monkeypatch.setenv(f"{prefix}_USER_AGENT", "user-agent")
    monkeypatch.setenv(f"{prefix}_DEVICE_MODEL", "device-model")
    monkeypatch.setenv(f"{prefix}_OS_VERSION", "os-version")

    lease = LocalAccountProvider(tmp_path).acquire(
        region, "worker", ttl_seconds=60, idempotency_key="request-1"
    )

    assert credential_to_account_info(lease.credential) == {
        "loginInfo": {"accessToken": "token"},
        "userId": "open-id",
        "deviceId": "device-id",
        "installId": "install-id",
        "userAgent": "user-agent",
        "deviceModel": "device-model",
        "osVersion": "os-version",
    }


def test_missing_environment_credentials_fail_without_exposing_values(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("SEKAI_TW_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SEKAI_TW_SDK_OPEN_ID", raising=False)
    monkeypatch.delenv("SEKAI_TW_DEVICE_ID", raising=False)
    monkeypatch.delenv("SEKAI_TW_INSTALL_ID", raising=False)
    monkeypatch.delenv("SEKAI_TW_USER_AGENT", raising=False)
    monkeypatch.delenv("SEKAI_TW_DEVICE_MODEL", raising=False)
    monkeypatch.delenv("SEKAI_TW_OS_VERSION", raising=False)

    with pytest.raises(ValueError, match="Missing"):
        LocalAccountProvider(tmp_path).acquire(
            AccountRegion.TW, "worker", ttl_seconds=60, idempotency_key="request-1"
        )


def test_invalid_yaml_credential_is_rejected_without_secret_in_error(tmp_path):
    account_path = tmp_path / "sharedAccount.jp.yaml"
    account_path.write_text("userId: '1'\ncredential: do-not-leak\n")

    with pytest.raises(ValueError) as caught:
        LocalAccountProvider(tmp_path).acquire(
            AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-1"
        )

    assert "do-not-leak" not in str(caught.value)


def test_acquire_is_idempotent_and_release_is_idempotent(tmp_path):
    (tmp_path / "sharedAccount.jp.yaml").write_text(
        "userId: '1'\ncredential: secret\nsignature: signed\n"
    )
    provider = LocalAccountProvider(tmp_path)
    first = provider.acquire(
        AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-1"
    )
    second = provider.acquire(
        AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-1"
    )
    same_consumer = provider.acquire(
        AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-2"
    )

    assert second is first
    assert same_consumer is first
    with pytest.raises(AccountUnavailableError):
        provider.acquire(
            AccountRegion.JP,
            "another-worker",
            ttl_seconds=60,
            idempotency_key="request-3",
        )
    provider.release(first.lease_id)
    provider.release(first.lease_id)


def test_report_invalid_releases_known_lease_and_rejects_unknown(tmp_path):
    (tmp_path / "sharedAccount.jp.yaml").write_text(
        "userId: '1'\ncredential: secret\nsignature: signed\n"
    )
    provider = LocalAccountProvider(tmp_path)
    lease = provider.acquire(
        AccountRegion.JP, "worker", ttl_seconds=60, idempotency_key="request-1"
    )

    provider.report_invalid(lease.lease_id, InvalidAccountReason.UNKNOWN)
    with pytest.raises(InvalidLeaseError):
        provider.report_invalid(lease.lease_id, InvalidAccountReason.UNKNOWN)
