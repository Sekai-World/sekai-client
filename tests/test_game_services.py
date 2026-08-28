"""Tests for high-level game API services."""

from unittest.mock import Mock

import pytest

from game_services import GameAPIService, PublicGameAPIService


def test_user_service_owns_user_endpoint_construction():
    caller = Mock()
    caller.call_pjsk_api.return_value = {"name": "user"}
    service = GameAPIService(caller, "self-user")

    assert service.fetch_user_profile("jp", "target-user") == {"name": "user"}
    caller.call_pjsk_api.assert_called_once_with("/user/self-user/target-user/profile")


def test_user_service_includes_account_id_in_suite_user_endpoint():
    caller = Mock()
    caller.call_pjsk_api.return_value = {"userId": "self-user"}
    service = GameAPIService(caller, "self-user")

    assert service.fetch_suite_user() == {"userId": "self-user"}
    caller.call_pjsk_api.assert_called_once_with("/suite/user/self-user")


def test_public_service_owns_non_user_endpoints():
    caller = Mock()
    caller.call_pjsk_api.return_value = {
        "maintenanceStatus": "none",
        "appVersions": [
            {
                "appVersion": "1.0.0",
                "dataVersion": "1.0.0",
                "assetVersion": "1.0.0",
                "appVersionStatus": "available",
            }
        ],
    }

    assert PublicGameAPIService(caller).fetch_system_data() == {
        "maintenanceStatus": "none",
        "appVersions": [
            {
                "appVersion": "1.0.0",
                "dataVersion": "1.0.0",
                "assetVersion": "1.0.0",
                "appVersionStatus": "available",
            }
        ],
    }
    caller.call_pjsk_api.assert_called_once_with("/system")


def test_service_rejects_non_object_response():
    caller = Mock()
    caller.call_pjsk_api.return_value = b"unexpected"

    with pytest.raises(RuntimeError, match="Expected object response"):
        GameAPIService(caller, "user").fetch_suite_user()
