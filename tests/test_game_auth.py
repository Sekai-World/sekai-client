"""Tests for the lifecycle-independent authentication session service."""

from unittest.mock import Mock

import pytest

from accounts import AccountRegion, JpEnCredential, TwKrCredential
from game_auth import GameAuthenticationService


def test_jp_authentication_returns_session_metadata():
    transport = Mock()
    transport.call_pjsk_api.return_value = {
        "sessionToken": "session",
        "suiteMasterSplitPath": ["master/a"],
    }
    credential = JpEnCredential(AccountRegion.JP, "user", "credential", "signature")

    result = GameAuthenticationService(transport).authenticate(credential)

    assert result.master_split_paths == ("master/a",)
    transport.call_pjsk_api.assert_called_once_with(
        "/user/user/auth?refreshUpdatedResources=False",
        "put",
        {"credential": "credential"},
    )


def test_kr_authentication_uses_access_token():
    transport = Mock()
    transport.call_pjsk_api.return_value = {"sessionToken": "session"}
    credential = TwKrCredential(AccountRegion.KR, "open-id", "access-token")

    GameAuthenticationService(transport).authenticate(credential)

    transport.call_pjsk_api.assert_called_once_with(
        "/user/auth", "post", {"userID": 0, "accessToken": "access-token"}
    )


@pytest.mark.parametrize("response", [None, b"data", {}, {"sessionToken": ""}])
def test_authentication_rejects_invalid_response(response):
    transport = Mock()
    transport.call_pjsk_api.return_value = response
    credential = TwKrCredential(AccountRegion.TW, "open-id", "access-token")

    with pytest.raises(ValueError, match="Invalid credential validation response"):
        GameAuthenticationService(transport).authenticate(credential)
