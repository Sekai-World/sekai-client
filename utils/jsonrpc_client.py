"""
JSON-RPC client for communicating with sekai-client servers.

Provides a simple wrapper around requests to make JSON-RPC calls
to local sekai-client JSON-RPC servers.
"""

from ipaddress import IPv6Address, ip_address
from typing import Any
from urllib.parse import urlparse

import requests
from jsonrpcclient.requests import request_uuid
from jsonrpcclient.responses import Error, Ok, parse

from config import Config

# Header used to authenticate internal JSON-RPC calls between the
# sekai-client processes (shared_client / check_update / event_tracker /
# api_public_server). All such calls must run on loopback only.
INTERNAL_RPC_TOKEN_HEADER = "x-internal-rpc-token"
INTERNAL_RPC_TIMEOUT_HEADER = "x-internal-rpc-timeout-ms"


class JSONRPCClient:
    """
    Client for making JSON-RPC method calls to sekai-client servers.

    Wraps the requests library to provide convenient JSON-RPC request/response
    handling with automatic timeout configuration. Every request carries the
    internal RPC auth token (read dynamically from the environment so it can be
    rotated without a restart); loopback dev runs without a token are allowed
    via ALLOW_INSECURE_INTERNAL_RPC.

    Attributes:
        url: Base URL of the JSON-RPC server (e.g., 'http://localhost:39390/')
    """

    def __init__(self, url: str = "http://localhost:39390/") -> None:
        """
        Initialize a JSON-RPC client.

        Args:
            url: Base URL of the JSON-RPC server endpoint
        """
        self.url = url

    def _is_loopback_target(self, url: str | None = None) -> bool:
        """Whether ``url`` points at a loopback host.

        Besides the usual names and addresses, treat IPv4-mapped IPv6
        loopback addresses (for example ``::ffff:127.0.0.1``) as loopback.
        This keeps the token boundary consistent across equivalent local URL
        spellings.
        """
        target_url = self.url if url is None else url
        try:
            host = (urlparse(target_url).hostname or "").rstrip(".").lower()
        except ValueError:
            return False
        if host == "localhost":
            return True

        try:
            address = ip_address(host)
        except ValueError:
            return False

        if address.is_loopback:
            return True
        return (
            isinstance(address, IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        )

    def _build_headers(self, url: str | None = None) -> dict[str, str]:
        """Build request headers, including the internal RPC auth token.

        Fails closed: if no token is configured and dev bypass is disabled,
        raise so the client never talks to the server unauthenticated. Dev
        loopback runs may omit the token via ALLOW_INSECURE_INTERNAL_RPC.
        The token is only ever sent to loopback targets, so it cannot
        leak to an external URL.
        """
        target_url = self.url if url is None else url
        headers: dict[str, str] = {}
        token = Config.get_internal_rpc_token()
        if token:
            if not self._is_loopback_target(target_url):
                raise RuntimeError(
                    "Refusing to send INTERNAL_RPC_TOKEN to a non-loopback target"
                )
            headers[INTERNAL_RPC_TOKEN_HEADER] = token
            return headers
        if Config.allow_insecure_internal_rpc() and self._is_loopback_target(
            target_url
        ):
            return headers
        raise RuntimeError(
            "INTERNAL_RPC_TOKEN is not configured; cannot issue internal "
            "RPC request (set INTERNAL_RPC_TOKEN or ALLOW_INSECURE_INTERNAL_RPC "
            "for loopback dev only)"
        )

    def request(
        self,
        func_name: str,
        params: list[Any] | tuple[Any, ...] | dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """
        Make a JSON-RPC method call.

        Automatically attaches the internal RPC auth token and fails closed when
        no token is configured (unless loopback dev bypass is enabled). The
        token is only attached for loopback targets.  Callers may provide a
        narrow per-request timeout; the configured default remains unchanged.

        Args:
            func_name: Name of the remote method to call
            params: Parameters to pass (can be tuple, dict, or None)
            timeout: Optional timeout for this request only.  If omitted,
                ``Config.REQUEST_TIMEOUT`` is used.

        Returns:
            The result from the JSON-RPC response

        Raises:
            RuntimeError: If no internal RPC token is configured, if the
                token would be sent to a non-loopback URL, or if the
                JSON-RPC response is an error
        """
        request_params = tuple(params) if isinstance(params, list) else params

        # Keep authorization and transport tied to one immutable per-request
        # target.  In particular, a dynamic token read must not observe a URL
        # changed concurrently (or by a callback) after authorization.
        target_url = self.url
        headers = self._build_headers(target_url)
        request_timeout = Config.REQUEST_TIMEOUT if timeout is None else timeout
        if request_timeout <= 0:
            raise ValueError("JSON-RPC timeout must be positive")
        headers[INTERNAL_RPC_TIMEOUT_HEADER] = str(max(1, int(request_timeout * 1000)))
        try:
            r = requests.post(
                target_url,
                json=request_uuid(func_name, request_params),
                headers=headers,
                timeout=request_timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("JSON-RPC request failed") from None

        status_code = r.status_code
        if isinstance(status_code, int) and 300 <= status_code < 400:
            raise RuntimeError(f"JSON-RPC redirect refused (HTTP {status_code})")

        # Surface HTTP errors (auth rejection, 5xx) before attempting to parse,
        # without retaining requests' URL/body-bearing exception text.
        try:
            r.raise_for_status()
        except requests.RequestException:
            raise RuntimeError(f"JSON-RPC HTTP error (HTTP {status_code})") from None

        try:
            payload = r.json()
            parsed = parse(payload)
        except Exception:
            # Do not include response text or parser details: either can carry
            # upstream JSON-RPC messages, data, or credentials.
            raise RuntimeError("Invalid JSON-RPC response") from None

        if isinstance(parsed, Ok):
            return parsed.result

        if not isinstance(parsed, Error):
            raise RuntimeError("Batch JSON-RPC responses are not supported")

        error_code = getattr(parsed, "code", None)
        if isinstance(error_code, int) and not isinstance(error_code, bool):
            # Error codes are useful for callers, but never expose the
            # upstream message or data and keep the context bounded.
            raise RuntimeError(f"JSON-RPC error (code={str(error_code)[:32]})")
        raise RuntimeError("JSON-RPC error")

    @property
    def url(self) -> str:
        """Get the server URL."""
        return self._url

    @url.setter
    def url(self, data: str) -> None:
        """Set the server URL."""
        self._url = data
