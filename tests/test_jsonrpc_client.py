"""
Unit tests for JSON-RPC client.

Tests remote method calls and error handling.
"""

from unittest.mock import Mock, patch

import pytest

from config import Config
from utils.jsonrpc_client import JSONRPCClient


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

        assert "Method not found" in str(excinfo.value)

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
