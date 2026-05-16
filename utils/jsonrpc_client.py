"""
JSON-RPC client for communicating with sekai-client servers.

Provides a simple wrapper around requests to make JSON-RPC calls
to local sekai-client JSON-RPC servers.
"""

from typing import Any

import requests
from jsonrpcclient.requests import request_uuid
from jsonrpcclient.responses import Ok, parse

from config import Config


class JSONRPCClient:
    """
    Client for making JSON-RPC method calls to sekai-client servers.

    Wraps the requests library to provide convenient JSON-RPC request/response
    handling with automatic timeout configuration.

    Attributes:
        url: Base URL of the JSON-RPC server (e.g., 'http://localhost:3939/')
    """

    def __init__(self, url: str = "http://localhost:3939/") -> None:
        """
        Initialize a JSON-RPC client.

        Args:
            url: Base URL of the JSON-RPC server endpoint
        """
        self.url = url

    def request(
        self, func_name: str, params: tuple[Any, ...] | dict[str, Any] | None = None
    ) -> Any:
        """
        Make a JSON-RPC method call.

        Args:
            func_name: Name of the remote method to call
            params: Parameters to pass (can be tuple, dict, or None)

        Returns:
            The result from the JSON-RPC response

        Raises:
            RuntimeError: If the JSON-RPC response is an error
        """
        r = requests.post(
            self.url,
            json=request_uuid(func_name, params),
            timeout=Config.REQUEST_TIMEOUT,
        )
        parsed = parse(r.json())

        if isinstance(parsed, Ok):
            return parsed.result
        else:
            raise RuntimeError(parsed.message)

    @property
    def url(self) -> str:
        """Get the server URL."""
        return self._url

    @url.setter
    def url(self, data: str) -> None:
        """Set the server URL."""
        self._url = data
