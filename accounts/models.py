"""Typed account credentials and lease values shared by account providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class AccountRegion(StrEnum):
    JP = "jp"
    EN = "en"
    TW = "tw"
    KR = "kr"


class InvalidAccountReason(StrEnum):
    CREDENTIAL_INVALID = "credential_invalid"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCOUNT_RESTRICTED = "account_restricted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, repr=False)
class JpEnCredential:
    region: AccountRegion
    user_id: str
    credential: str = field(repr=False)
    signature: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.region not in (AccountRegion.JP, AccountRegion.EN):
            raise ValueError("JP/EN credential requires region jp or en")
        if not self.user_id or not self.credential or not self.signature:
            raise ValueError("JP/EN credential fields must be non-empty")

    def __repr__(self) -> str:
        return f"JpEnCredential(region={self.region.value!r})"


@dataclass(frozen=True, repr=False)
class TwKrCredential:
    region: AccountRegion
    sdk_open_id: str
    access_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.region not in (AccountRegion.TW, AccountRegion.KR):
            raise ValueError("TW/KR credential requires region tw or kr")
        if not self.sdk_open_id or not self.access_token:
            raise ValueError("TW/KR credential fields must be non-empty")

    def __repr__(self) -> str:
        return f"TwKrCredential(region={self.region.value!r})"


type AccountCredential = JpEnCredential | TwKrCredential


@dataclass(frozen=True)
class AccountLease:
    lease_id: str
    consumer: str
    expires_at: datetime
    credential: AccountCredential = field(repr=False)

    def __post_init__(self) -> None:
        if not self.lease_id or not self.consumer:
            raise ValueError("lease id and consumer must be non-empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("lease expiry must be timezone-aware")
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))

    @property
    def region(self) -> AccountRegion:
        return self.credential.region

    def is_expired(self, now: datetime | None = None) -> bool:
        observed_at = datetime.now(UTC) if now is None else now
        if observed_at.tzinfo is None:
            raise ValueError("expiry comparison time must be timezone-aware")
        return observed_at.astimezone(UTC) >= self.expires_at
