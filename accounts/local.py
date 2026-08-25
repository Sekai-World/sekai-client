"""Local account provider backed by the existing YAML and environment sources."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import uuid4

import yaml

from accounts.models import (
    AccountLease,
    AccountRegion,
    InvalidAccountReason,
    JpEnCredential,
    TwKrCredential,
)
from accounts.provider import AccountUnavailableError, InvalidLeaseError

logger = logging.getLogger(__name__)

RegistrationCallback = Callable[[AccountRegion], JpEnCredential]


def credential_to_account_info(
    credential: JpEnCredential | TwKrCredential,
) -> dict[str, object]:
    """Convert a typed credential to the game client's legacy login payload."""
    if isinstance(credential, JpEnCredential):
        return {
            "signature": credential.signature,
            "credential": credential.credential,
            "userId": credential.user_id,
        }
    return {
        "loginInfo": {"accessToken": credential.access_token},
        "userId": credential.sdk_open_id,
        "deviceId": credential.device_id,
    }


class LocalAccountProvider:
    """Preserve existing local credential behavior behind `AccountProvider`."""

    def __init__(
        self,
        base_dir: str | Path,
        register_account: RegistrationCallback | None = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._register_account = register_account
        self._leases: dict[str, AccountLease] = {}
        self._idempotency: dict[str, tuple[AccountRegion, str, AccountLease]] = {}
        self._lock = Lock()

    def acquire(
        self,
        region: AccountRegion,
        consumer: str,
        *,
        ttl_seconds: int,
        idempotency_key: str,
    ) -> AccountLease:
        if not consumer or not idempotency_key or ttl_seconds <= 0:
            raise ValueError("consumer, idempotency key, and positive TTL are required")

        with self._lock:
            previous = self._idempotency.get(idempotency_key)
            if previous is not None:
                previous_region, previous_consumer, lease = previous
                if previous_region != region or previous_consumer != consumer:
                    raise InvalidLeaseError()
                if not lease.is_expired():
                    return lease

            for lease in self._leases.values():
                if lease.region is not region or lease.is_expired():
                    continue
                if lease.consumer != consumer:
                    raise AccountUnavailableError()
                self._idempotency[idempotency_key] = (region, consumer, lease)
                return lease

            credential = self._load_credential(region)
            lease = AccountLease(
                lease_id=f"local-{uuid4()}",
                consumer=consumer,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                credential=credential,
            )
            self._leases[lease.lease_id] = lease
            self._idempotency[idempotency_key] = (region, consumer, lease)
            return lease

    def release(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)
            self._idempotency = {
                key: value
                for key, value in self._idempotency.items()
                if value[2].lease_id != lease_id
            }

    def report_invalid(self, lease_id: str, reason: InvalidAccountReason) -> None:
        del reason
        with self._lock:
            if lease_id not in self._leases:
                raise InvalidLeaseError()
        self.release(lease_id)

    def _load_credential(
        self, region: AccountRegion
    ) -> JpEnCredential | TwKrCredential:
        if region in (AccountRegion.JP, AccountRegion.EN):
            return self._load_jp_en_credential(region)
        return self._load_tw_kr_credential(region)

    def _load_jp_en_credential(self, region: AccountRegion) -> JpEnCredential:
        account_path = self._base_dir / f"sharedAccount.{region.value}.yaml"
        if account_path.exists():
            _enforce_private_permissions(account_path)
            with account_path.open(encoding="utf-8") as account_file:
                account_info = yaml.safe_load(account_file)
            if not isinstance(account_info, dict):
                raise ValueError(f"Invalid account info file: {account_path}")
        else:
            if self._register_account is None:
                raise ValueError(f"No local account available for {region.value}")
            logger.warning("no %s account found, registering a new one", region.value)
            registered = self._register_account(region)
            if registered.region is not region:
                raise ValueError(
                    "Registration returned a credential for another region"
                )
            account_info = credential_to_account_info(registered)
            _write_account_yaml_atomic(account_path, account_info)

        try:
            return JpEnCredential(
                region=region,
                user_id=str(account_info["userId"]),
                credential=str(account_info["credential"]),
                signature=str(account_info["signature"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid account info file: {account_path}") from error

    @staticmethod
    def _load_tw_kr_credential(region: AccountRegion) -> TwKrCredential:
        prefix = f"SEKAI_{region.value.upper()}"
        access_token = os.getenv(f"{prefix}_ACCESS_TOKEN")
        sdk_open_id = os.getenv(f"{prefix}_SDK_OPEN_ID")
        device_id = os.getenv(f"{prefix}_DEVICE_ID")
        if not access_token or not sdk_open_id or not device_id:
            raise ValueError(
                f"Missing access token, SDK open id, or device id "
                f"for {region.value} server"
            )
        return TwKrCredential(
            region=region,
            sdk_open_id=sdk_open_id,
            access_token=access_token,
            device_id=device_id,
        )


def _enforce_private_permissions(account_path: Path) -> None:
    try:
        if account_path.stat().st_mode & 0o077:
            account_path.chmod(0o600)
    except OSError:
        pass


def _write_account_yaml_atomic(
    account_path: Path, account_info: dict[str, object]
) -> None:
    fd, temporary_name = tempfile.mkstemp(
        dir=account_path.parent, prefix=".sharedAccount.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as account_file:
            yaml.safe_dump(account_info, account_file)
            account_file.flush()
            os.fsync(account_file.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(account_path)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise
