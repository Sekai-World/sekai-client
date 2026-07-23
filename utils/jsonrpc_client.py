"""
JSON-RPC client for communicating with sekai-client servers.

Provides a simple wrapper around requests to make JSON-RPC calls
to local sekai-client JSON-RPC servers.
"""

from typing import Any

import requests
from jsonrpcclient.requests import request_uuid
from jsonrpcclient.responses import Error, Ok, parse

from config import Config

# Header used to authenticate internal JSON-RPC calls between the
# sekai-client processes (shared_client / check_update / event_tracker /
# api_public_server). All such calls must run on loopback only.
INTERNAL_RPC_TOKEN_HEADER = "x-internal-rpc-token"


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

    def _is_loopback_target(self) -> bool:
        """Whether this client's URL points at loopback (127.0.0.1 / ::1)."""
        from urllib.parse import urlparse

        host = urlparse(self.url).hostname or ""
        return host in ("127.0.0.1", "::1", "localhost")

    def _build_headers(self) -> dict[str, str]:
        """Build request headers, including the internal RPC auth token.

        Fails closed: if no token is configured and dev bypass is disabled,
        raise so the client never talks to the server unauthenticated. Dev
        loopback runs may omit the token via ALLOW_INSECURE_INTERNAL_RPC.
        The token is only ever sent to loopback targets, so it cannot
        leak to an external URL.
        """
        headers: dict[str, str] = {}
        token = Config.get_internal_rpc_token()
        if token:
            if not self._is_loopback_target():
                raise RuntimeError(
                    "Refusing to send INTERNAL_RPC_TOKEN to a non-loopback "
                    f"URL: {self.url!r}"
                )
            headers[INTERNAL_RPC_TOKEN_HEADER] = token
            return headers
        if Config.allow_insecure_internal_rpc() and self._is_loopback_target():
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

        headers = self._build_headers()
        r = requests.post(
            self.url,
            json=request_uuid(func_name, request_params),
            headers=headers,
            timeout=Config.REQUEST_TIMEOUT if timeout is None else timeout,
        )
        # Surface HTTP errors (auth rejection, 5xx) before attempting to parse.
        r.raise_for_status()

        try:
            payload = r.json()
        except ValueError as err:
            raise RuntimeError(f"Invalid JSON-RPC response: {r.text}") from err

        parsed = parse(payload)

        if isinstance(parsed, Ok):
            return parsed.result

        if not isinstance(parsed, Error):
            raise RuntimeError("Batch JSON-RPC responses are not supported")

        error_message = parsed.message
        error_code = getattr(parsed, "code", None)
        error_data = getattr(parsed, "data", None)
        if error_code is not None:
            error_message = f"{error_code}: {error_message}"
        if error_data is not None:
            error_message = f"{error_message} data={error_data!r}"
        raise RuntimeError(error_message)

    @property
    def url(self) -> str:
        """Get the server URL."""
        return self._url

    @url.setter
    def url(self, data: str) -> None:
        """Set the server URL."""
        self._url = data
