from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from accounts import (
    AccountLease,
    AccountProvider,
    AccountRegion,
    AccountUnavailableError,
    InvalidAccountReason,
    JpEnCredential,
    TwKrCredential,
)


def test_jp_en_credential_rejects_wrong_region_and_hides_secrets():
    with pytest.raises(ValueError, match="region jp or en"):
        JpEnCredential(AccountRegion.TW, "user", "credential", "signature")

    value = JpEnCredential(AccountRegion.JP, "user", "secret-cred", "secret-sig")
    rendered = repr(value)
    assert "secret-cred" not in rendered
    assert "secret-sig" not in rendered


def test_jp_en_credential_accepts_complete_fingerprint_and_hides_it():
    value = JpEnCredential(
        AccountRegion.JP,
        "user",
        "secret-cred",
        "secret-sig",
        install_id="lease-install",
        x_if="lease-if",
        x_kc="lease-kc",
        device_model="lease-model",
        os_version="lease-os",
        user_agent="lease-agent",
        issued_at=datetime(2026, 9, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )

    assert value.has_device_fingerprint is True
    rendered = repr(value)
    assert "lease-install" not in rendered
    assert "lease-agent" not in rendered


@pytest.mark.parametrize(
    "empty_field",
    ["install_id", "x_if", "x_kc", "device_model", "os_version", "user_agent"],
)
def test_jp_en_credential_rejects_partial_fingerprint(empty_field):
    fields = {
        "install_id": "lease-install",
        "x_if": "lease-if",
        "x_kc": "lease-kc",
        "device_model": "lease-model",
        "os_version": "lease-os",
        "user_agent": "lease-agent",
    }
    fields[empty_field] = ""

    with pytest.raises(ValueError, match="all-or-nothing"):
        JpEnCredential(AccountRegion.JP, "user", "credential", "signature", **fields)


def test_jp_en_credential_without_fingerprint_is_legacy_compatible():
    value = JpEnCredential(AccountRegion.EN, "user", "credential", "signature")

    assert value.has_device_fingerprint is False
    assert value.issued_at is None
    assert value.expires_at is None


def test_jp_en_credential_normalizes_timestamps_to_utc():
    value = JpEnCredential(
        AccountRegion.JP,
        "user",
        "credential",
        "signature",
        issued_at=datetime(2026, 9, 9, 12, 0, tzinfo=timezone(timedelta(hours=9))),
        expires_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone(timedelta(hours=9))),
    )

    assert value.issued_at is not None
    assert value.issued_at.tzinfo is UTC
    assert value.issued_at.hour == 3
    assert value.expires_at is not None
    assert value.expires_at.tzinfo is UTC


def test_jp_en_credential_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        JpEnCredential(
            AccountRegion.JP,
            "user",
            "credential",
            "signature",
            issued_at=datetime(2026, 9, 9),
        )


class _OffsetlessTz(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return None


def test_jp_en_credential_rejects_offsetless_tzinfo():
    with pytest.raises(ValueError, match="timezone-aware"):
        JpEnCredential(
            AccountRegion.JP,
            "user",
            "credential",
            "signature",
            expires_at=datetime(2026, 9, 9, 12, tzinfo=_OffsetlessTz()),
        )


def test_tw_kr_credential_rejects_wrong_region_and_hides_token():
    with pytest.raises(ValueError, match="region tw or kr"):
        TwKrCredential(
            AccountRegion.EN,
            "open-id",
            "access-token",
            "device-id",
            "install-id",
            "user-agent",
            "device-model",
            "os-version",
        )

    value = TwKrCredential(
        AccountRegion.KR,
        "open-id",
        "secret-token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "os-version",
    )
    assert "secret-token" not in repr(value)


def test_account_lease_normalizes_expiry_and_hides_credential():
    credential = JpEnCredential(AccountRegion.EN, "user", "secret-cred", "secret-sig")
    expires_at = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    lease = AccountLease("lease-1", "event-tracker-en", expires_at, credential)

    assert lease.region is AccountRegion.EN
    assert lease.expires_at.tzinfo is UTC
    assert "secret-cred" not in repr(lease)
    assert lease.is_expired(expires_at - timedelta(seconds=1)) is False
    assert lease.is_expired(expires_at) is True


def test_account_lease_rejects_naive_expiry():
    credential = TwKrCredential(
        AccountRegion.TW,
        "open-id",
        "access-token",
        "device-id",
        "install-id",
        "user-agent",
        "device-model",
        "os-version",
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        AccountLease("lease-1", "consumer", datetime(2026, 8, 13), credential)


def test_provider_error_is_stable_and_credential_safe():
    error = AccountUnavailableError(retry_after=2.5)
    assert str(error) == "account_unavailable"
    assert error.code == "account_unavailable"
    assert error.retryable is True
    assert error.retry_after == 2.5


def test_structural_provider_contract():
    credential = JpEnCredential(AccountRegion.JP, "user", "credential", "signature")
    lease = AccountLease(
        "lease-1",
        "test-consumer",
        datetime.now(UTC) + timedelta(minutes=5),
        credential,
    )

    class FakeProvider:
        def acquire(self, region, consumer, *, ttl_seconds, idempotency_key):
            assert region is AccountRegion.JP
            assert consumer == "test-consumer"
            assert ttl_seconds == 300
            assert idempotency_key == "request-1"
            return lease

        def renew(self, lease_id, *, extend_seconds, idempotency_key):
            raise NotImplementedError

        def release(self, lease_id):
            assert lease_id == "lease-1"

        def report_invalid(self, lease_id, reason):
            assert lease_id == "lease-1"
            assert reason is InvalidAccountReason.CREDENTIAL_INVALID

    provider: AccountProvider = FakeProvider()
    acquired = provider.acquire(
        AccountRegion.JP,
        "test-consumer",
        ttl_seconds=300,
        idempotency_key="request-1",
    )
    provider.report_invalid(acquired.lease_id, InvalidAccountReason.CREDENTIAL_INVALID)
    provider.release(acquired.lease_id)
    assert acquired is lease
