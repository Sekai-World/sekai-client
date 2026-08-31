"""Provider boundary for local and future remote account acquisition."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from accounts.models import AccountLease, AccountRegion, InvalidAccountReason


class AccountProviderError(RuntimeError):
    """Credential-safe provider failure with stable machine-readable fields."""

    def __init__(
        self, code: str, *, retryable: bool, retry_after: float | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class AccountUnavailableError(AccountProviderError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("account_unavailable", retryable=True, retry_after=retry_after)


class InvalidLeaseError(AccountProviderError):
    def __init__(self) -> None:
        super().__init__("invalid_lease", retryable=False)


class AccountProvider(Protocol):
    """Acquire exclusive leases without exposing provider implementation details.

    `idempotency_key` identifies one logical acquire operation and must be reused
    across safe retries. Providers must return the same live lease for repeated
    acquire calls with the same key and request identity. Release is idempotent.
    Expired leases may be reclaimed by the provider; long-running consumers must
    reacquire before using an expired lease.
    """

    def acquire(
        self,
        region: AccountRegion,
        consumer: str,
        *,
        ttl_seconds: int,
        idempotency_key: str,
    ) -> AccountLease: ...

    def renew(
        self,
        lease_id: str,
        *,
        extend_seconds: int,
        idempotency_key: str,
    ) -> datetime:
        """Renew a remote lease without changing its lease ID.

        Returns the renewed timezone-aware expiration. The lease ID is unchanged.
        `idempotency_key` identifies one logical renewal and must be reused across
        safe retries. A 404 maps to `InvalidLeaseError`. Implementers that do not
        support renewal should raise `NotImplementedError`; local providers remain
        reacquire-only.
        """
        ...

    def release(self, lease_id: str) -> None: ...

    def report_invalid(self, lease_id: str, reason: InvalidAccountReason) -> None: ...
