"""Generic HTTP request execution, retry policy, and backoff helpers."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from time import sleep
from typing import Any
from uuid import uuid4

import requests

from utils.deadline import DeadlineExceeded, current_deadline

type APIResponse = bytes | dict[str, Any] | None
type EncryptRequestBody = Callable[[str, str | dict], bytes | None]
type SendRequest = Callable[[str, str, bytes | None, str], requests.Response]
type DecryptResponse = Callable[[requests.Response], APIResponse]
type RecoveryHandler = Callable[[requests.Response | None, Any], bool]
type RetryWaiter = Callable[[requests.Response | None, int], None]


class RetryPolicy(StrEnum):
    """Whether a logical request may be repeated safely."""

    NEVER = "never"
    IDEMPOTENT = "idempotent"


def retry_after_seconds(
    response: requests.Response | None,
    *,
    now: Callable[[], datetime] | None = None,
) -> float | None:
    """Parse numeric or HTTP-date ``Retry-After`` values for 429 responses."""
    if response is None or response.status_code != 429:
        return None
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current_time = now() if now is not None else datetime.now(UTC)
        return max(0.0, (retry_at - current_time).total_seconds())


def wait_before_retry(
    response: requests.Response | None,
    attempt: int,
    *,
    get_retry_after: Callable[[requests.Response | None], float | None],
    set_rate_limited: Callable[[bool], None],
    sleep_fn: Callable[[float], None] = sleep,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> None:
    """Apply bounded exponential-jitter backoff or an upstream retry delay."""
    retry_after = get_retry_after(response)
    if retry_after is None:
        base = min(30.0, 2.0 ** (attempt - 1))
        delay = jitter_fn(base * 0.5, base * 1.5)
    else:
        delay = retry_after

    deadline = current_deadline()
    if deadline is not None and delay >= deadline.remaining():
        raise DeadlineExceeded("Request deadline exceeded")

    rate_limited = response is not None and response.status_code == 429
    if rate_limited:
        set_rate_limited(True)
    try:
        sleep_fn(delay)
    finally:
        if rate_limited:
            set_rate_limited(False)


def execute_request(
    endpoint: str,
    method: str,
    body: str | dict,
    retry_policy: RetryPolicy | str | None,
    *,
    max_retries: int,
    logger: logging.Logger,
    encrypt_request_body: EncryptRequestBody,
    send_request: SendRequest,
    decrypt_response: DecryptResponse,
    recovery_handler: RecoveryHandler | None,
    wait_for_retry: RetryWaiter,
) -> APIResponse:
    """Execute one logical request while keeping transport/retry policy generic."""
    normalized_method = method.lower()
    policy = (
        RetryPolicy(retry_policy)
        if retry_policy is not None
        else (
            RetryPolicy.IDEMPOTENT if normalized_method == "get" else RetryPolicy.NEVER
        )
    )
    retry_limit = max_retries if policy is RetryPolicy.IDEMPOTENT else 0
    data = encrypt_request_body(normalized_method, body)
    request_id = str(uuid4())
    logger.debug(
        "call_pjsk_api endpoint=%s method=%s request_id=%s body=%s encrypted_len=%s",
        endpoint,
        normalized_method,
        request_id,
        "<redacted>" if body else "",
        len(data) if data is not None else None,
    )

    attempt = 0
    while True:
        response: requests.Response | None = None
        response_data: APIResponse = None
        try:
            response = send_request(endpoint, normalized_method, data, request_id)
            if 300 <= response.status_code < 400:
                raise requests.HTTPError(response=response)
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise requests.HTTPError(response=response)
            response_data = decrypt_response(response)
            return response_data
        except requests.HTTPError:
            if (
                response is not None
                and not 300 <= response.status_code < 400
                and not 200 <= response.status_code < 300
            ):
                try:
                    response_data = decrypt_response(response)
                except Exception:
                    # Error-body decoding is best-effort. Keep the HTTP error
                    # path sanitized if the payload is absent or malformed.
                    response_data = None

            status_code = response.status_code if response is not None else "unknown"
            logger.error(
                "Request PJSK api error, endpoint=%s, method=%s, body=%s, status=%s",
                endpoint,
                method,
                "<redacted>",
                status_code,
            )

            handled = (
                recovery_handler(response, response_data)
                if recovery_handler is not None
                else False
            )
            should_retry = policy is RetryPolicy.IDEMPOTENT and handled
            transient = response is not None and (
                response.status_code == 429 or 500 <= response.status_code < 600
            )
            if (should_retry or transient) and attempt < retry_limit:
                attempt += 1
                wait_for_retry(response, attempt)
                continue

            raise RuntimeError(
                f"PJSK API request failed (HTTP {status_code})"
            ) from None
        except requests.RequestException as error:
            logger.error(
                "Request PJSK api request exception, endpoint=%s, method=%s, error=%s",
                endpoint,
                method,
                error,
            )
            if attempt < retry_limit:
                attempt += 1
                wait_for_retry(None, attempt)
                continue
            raise RuntimeError("PJSK API request failed") from None
