"""HTTP account provider for the separately deployed account service."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

import requests

from accounts.models import (
    AccountLease,
    AccountRegion,
    InvalidAccountReason,
    JpEnCredential,
    TwKrCredential,
)
from accounts.provider import (
    AccountProviderError,
    AccountUnavailableError,
    InvalidLeaseError,
)


class RemoteAccountProvider:
    requires_durable_idempotency = True

    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout: float = 10.0,
        max_attempts: int = 3,
        session: Any | None = None,
        sleep=time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("account service URL is invalid")
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("account service URL must use HTTPS outside loopback")
        if not service_token:
            raise ValueError("account service token is required")
        if timeout <= 0 or max_attempts <= 0:
            raise ValueError("positive timeout and attempt count are required")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}"}
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._session = session or requests.Session()
        self._sleep = sleep

    def acquire(
        self,
        region: AccountRegion,
        consumer: str,
        *,
        ttl_seconds: int,
        idempotency_key: str,
    ) -> AccountLease:
        response = self._request(
            "post",
            "/v1/leases",
            json={
                "region": region.value,
                "consumer": consumer,
                "ttl_seconds": ttl_seconds,
            },
            headers={"Idempotency-Key": idempotency_key},
            retry=True,
        )
        if response.status_code == 503:
            raise AccountUnavailableError(self._retry_after(response))
        self._raise_for_status(response)
        try:
            payload = response.json()
            if payload["region"] != region.value or payload["consumer"] != consumer:
                raise ValueError
            credential = self._credential(region, payload["auth"])
            expires_at = datetime.fromisoformat(payload["expires_at"])
            return AccountLease(
                lease_id=payload["lease_id"],
                consumer=consumer,
                expires_at=expires_at,
                credential=credential,
            )
        except (KeyError, TypeError, ValueError):
            raise AccountProviderError(
                "invalid_service_response", retryable=False
            ) from None

    def release(self, lease_id: str) -> None:
        response = self._request(
            "delete", f"/v1/leases/{quote(lease_id, safe='')}", retry=True
        )
        if response.status_code == 404:
            raise InvalidLeaseError
        self._raise_for_status(response)

    def report_invalid(self, lease_id: str, reason: InvalidAccountReason) -> None:
        response = self._request(
            "post",
            f"/v1/leases/{quote(lease_id, safe='')}/invalid",
            json={"reason": reason.value},
            retry=False,
        )
        if response.status_code == 404:
            raise InvalidLeaseError
        self._raise_for_status(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry: bool,
    ) -> Any:
        attempts = self._max_attempts if retry else 1
        request_headers = {**self._headers, **(headers or {})}
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json,
                    headers=request_headers,
                    timeout=self._timeout,
                )
            except requests.RequestException:
                if attempt == attempts:
                    raise AccountProviderError(
                        "account_service_unreachable", retryable=True
                    ) from None
                self._sleep(0.25 * attempt)
                continue
            if response.status_code not in (429, 502, 503, 504) or attempt == attempts:
                return response
            self._sleep(self._retry_after(response) or 0.25 * attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _credential(region: AccountRegion, payload: Any):
        if not isinstance(payload, dict):
            raise ValueError
        if region in (AccountRegion.JP, AccountRegion.EN):
            if payload.get("kind") != "jp_en":
                raise ValueError
            values = (
                payload.get("user_id"),
                payload.get("credential"),
                payload.get("signature"),
            )
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError
            return JpEnCredential(
                region,
                payload["user_id"],
                payload["credential"],
                payload["signature"],
            )
        if payload.get("kind") != "tw_kr":
            raise ValueError
        tw_kr_values = (
            payload.get("sdk_open_id"),
            payload.get("access_token"),
            payload.get("device_id"),
        )
        if not all(isinstance(value, str) and value for value in tw_kr_values):
            raise ValueError
        return TwKrCredential(
            region, payload["sdk_open_id"], payload["access_token"], payload["device_id"]
        )

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        try:
            value = float(response.headers.get("Retry-After", ""))
            return value if value >= 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raise_for_status(response: Any) -> None:
        if response.status_code in (401, 403):
            raise AccountProviderError("account_service_unauthorized", retryable=False)
        if response.status_code == 409:
            raise AccountProviderError("idempotency_conflict", retryable=False)
        if response.status_code == 429:
            raise AccountProviderError(
                "account_service_rate_limited",
                retryable=True,
                retry_after=RemoteAccountProvider._retry_after(response),
            )
        if response.status_code >= 500:
            raise AccountProviderError("account_service_failure", retryable=True)
        if response.status_code >= 400:
            raise AccountProviderError("account_service_rejected", retryable=False)
