"""Unit tests for lightweight API client metadata refresh."""

from unittest.mock import Mock

from api_client import APIClient


def test_refresh_master_split_paths_only_applies_auth_metadata():
    client = Mock()
    client.region = "jp"
    client.master_split_paths = ["master/path"]
    auth_data = {"sessionToken": "new-token"}
    client._authenticate.return_value = auth_data

    result = APIClient.refresh_master_split_paths(client)

    assert result == ["master/path"]
    client._authenticate.assert_called_once_with()
    client._apply_auth_headers_and_version_info.assert_called_once_with(auth_data)
    client.fetch_suite_user.assert_not_called()


def test_refresh_master_split_paths_rejects_regions_without_split_data():
    client = Mock()
    client.region = "cn"

    try:
        APIClient.refresh_master_split_paths(client)
    except ValueError as error:
        assert "only available for jp and en" in str(error)
    else:
        raise AssertionError("Expected split path refresh to reject cn")
