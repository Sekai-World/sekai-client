"""Bounded recovery for local shared-client JSON-RPC calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_OMITTED = object()


def request_with_recovery(
    client: Any,
    method: str,
    params: Any = _OMITTED,
    *,
    log_warning: Callable[[str, Any], Any] | None = None,
) -> Any:
    """Retry one failed call after one shared-client readiness recovery."""

    def request() -> Any:
        if params is _OMITTED:
            return client.request(method)
        return client.request(method, params)

    try:
        return request()
    except Exception:
        if method == "ensure_ready":
            raise
        if log_warning is not None:
            log_warning("RPC method failed; recovering method=%s", method)
        try:
            status = client.request("ensure_ready")
            if not isinstance(status, dict) or status.get("ready") is not True:
                raise RuntimeError(
                    "shared-client readiness recovery did not reach READY"
                )
        except Exception as error:
            raise RuntimeError(
                "RPC method failed and shared-client recovery failed"
            ) from error
        try:
            return request()
        except Exception as error:
            raise RuntimeError(
                "RPC method failed after shared-client recovery"
            ) from error
