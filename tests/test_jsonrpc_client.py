"""
Unit tests for JSON-RPC client.

Tests remote method calls and error handling.
"""

from unittest.mock import Mock, patch

import pytest
import requests

from config import Config
from utils.jsonrpc_client import JSONRPCClient


@pytest.fixture(autouse=True)
def _internal_rpc_token(monkeypatch):
    # The JSON-RPC client now requires an internal RPC token (fail-closed).
    # Provide a dev token so these request-level tests exercise the call path
    # without depending on real env config.
    monkeypatch.setattr(Config, "get_internal_rpc_token", lambda: "dev-token")
    monkeypatch.setattr(Config, "allow_insecure_internal_rpc", lambda: False)


class TestJSONRPCClient:
    """Tests for JSONRPCClient."""

    def test_init_with_default_url(self):
        """Test initialization with default URL."""
        client = JSONRPCClient()
        assert client.url == "http://localhost:39390/"

    def test_init_with_custom_url(self):
        """Test initialization with custom URL."""
        client = JSONRPCClient("http://localhost:39390/")
        assert client.url == "http://localhost:39390/"

    def test_url_property_getter(self):
        """Test URL property getter."""
        client = JSONRPCClient("http://test:1234/")
        assert client.url == "http://test:1234/"

    def test_url_property_setter(self):
        """Test URL property setter."""
        client = JSONRPCClient()
        client.url = "http://newhost:5678/"
        assert client.url == "http://newhost:5678/"

    @patch("requests.post")
    def test_request_with_success(self, mock_post):
        """Test successful JSON-RPC request."""
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"data": "test"},
            "id": 1,
        }
        mock_post.return_value = mock_response

        client = JSONRPCClient("http://localhost:39390/")
        result = client.request("test_method", ["param1", "param2"])

        assert result == {"data": "test"}
        mock_post.assert_called_once()

        # Verify timeout was used
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["timeout"] == Config.REQUEST_TIMEOUT

    @patch("requests.post")
    def test_request_with_dict_params(self, mock_post):
        """Test JSON-RPC request with dict parameters."""
        mock_response = Mock()
        mock_response.json.return_value = {"jsonrpc": "2.0", "result": "ok", "id": 1}
        mock_post.return_value = mock_response

        client = JSONRPCClient("http://localhost:39390/")
        result = client.request("test_method", {"key": "value"})

        assert result == "ok"

    @patch("requests.post")
    def test_request_with_none_params(self, mock_post):
        """Test JSON-RPC request with None parameters."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": "result",
            "id": 1,
        }
        mock_post.return_value = mock_response

        client = JSONRPCClient("http://localhost:39390/")
        result = client.request("test_method", None)

        assert result == "result"

    @patch("requests.post")
    def test_request_with_error_response(self, mock_post):
        """Test JSON-RPC request with error response."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "error": {"code": -1, "message": "Method not found"},
            "id": 1,
        }
        mock_post.return_value = mock_response

        client = JSONRPCClient("http://localhost:39390/")

        with pytest.raises(RuntimeError) as excinfo:
            client.request("nonexistent_method")

        assert "-1" in str(excinfo.value)
        assert "Method not found" not in str(excinfo.value)

    @patch("requests.post")
    def test_request_rejects_redirect_without_following(self, mock_post):
        response = Mock()
        response.status_code = 302
        response.json.side_effect = AssertionError("redirect must not be parsed")
        mock_post.return_value = response

        with pytest.raises(RuntimeError, match=r"redirect refused.*302"):
            JSONRPCClient().request("test")

        assert mock_post.call_args.kwargs["allow_redirects"] is False

    @patch("requests.post")
    def test_request_error_does_not_expose_upstream_message_data_or_body(
        self, mock_post
    ):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "jsonrpc": "2.0",
            "error": {
                "code": -32001,
                "message": "upstream-secret-message",
                "data": {"credential": "request-secret"},
            },
            "id": 1,
        }
        response.text = "raw-response-secret"
        mock_post.return_value = response

        with pytest.raises(RuntimeError) as excinfo:
            JSONRPCClient().request("method", {"token": "request-secret"})

        error = str(excinfo.value)
        assert "-32001" in error
        assert "upstream-secret-message" not in error
        assert "request-secret" not in error
        assert "raw-response-secret" not in error

    @patch("requests.post")
    def test_http_error_does_not_expose_upstream_details(self, mock_post):
        response = Mock(status_code=502)
        response.raise_for_status.side_effect = requests.HTTPError(
            "upstream-secret-response", response=response
        )
        response.text = "raw-response-secret"
        mock_post.return_value = response

        with pytest.raises(RuntimeError) as excinfo:
            JSONRPCClient().request("method")

        error = str(excinfo.value)
        assert error == "JSON-RPC HTTP error (HTTP 502)"
        assert "upstream-secret-response" not in error
        assert "raw-response-secret" not in error

    def test_loopback_includes_ipv4_mapped_ipv6(self):
        assert JSONRPCClient("http://[::ffff:127.0.0.1]:39390/")._is_loopback_target()

    @patch("requests.post")
    def test_request_uses_url_snapshot_for_auth_and_transport(
        self, mock_post, monkeypatch
    ):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"jsonrpc": "2.0", "result": "ok", "id": 1}
        mock_post.return_value = response
        client = JSONRPCClient("http://127.0.0.1:39390/")

        def get_token():
            client.url = "http://example.com:39390/?token=changed-secret"
            return "request-secret"

        monkeypatch.setattr(Config, "get_internal_rpc_token", get_token)

        assert client.request("test") == "ok"
        assert mock_post.call_args.args[0] == "http://127.0.0.1:39390/"
        assert mock_post.call_args.kwargs["headers"]["x-internal-rpc-token"] == (
            "request-secret"
        )

    @patch("requests.post")
    def test_request_uses_config_timeout(self, mock_post):
        """Test request uses CONFIG.REQUEST_TIMEOUT."""
        mock_response = Mock()
        mock_response.json.return_value = {"jsonrpc": "2.0", "result": "ok", "id": 1}
        mock_post.return_value = mock_response

        client = JSONRPCClient()
        client.request("test")

        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["timeout"] == Config.REQUEST_TIMEOUT

    @patch("requests.post")
    def test_request_uses_per_request_timeout_override(self, mock_post):
        mock_response = Mock()
        mock_response.json.return_value = {"jsonrpc": "2.0", "result": "ok", "id": 1}
        mock_post.return_value = mock_response

        JSONRPCClient().request("test", timeout=0.25)

        assert mock_post.call_args.kwargs["timeout"] == 0.25

    @patch("requests.post")
    def test_request_json_format(self, mock_post):
        """Test request uses JSON-RPC format."""
        mock_response = Mock()
        mock_response.json.return_value = {"jsonrpc": "2.0", "result": "ok", "id": 1}
        mock_post.return_value = mock_response

        client = JSONRPCClient("http://localhost:39390/")
        client.request("my_method", ["arg1", "arg2"])

        # Verify JSON-RPC structure
        call_kwargs = mock_post.call_args[1]
        assert "json" in call_kwargs
        json_data = call_kwargs["json"]
        assert json_data["jsonrpc"] == "2.0"
        assert json_data["method"] == "my_method"
