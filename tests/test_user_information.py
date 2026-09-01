"""Unit tests for check-update-independent user-information helpers."""

from unittest.mock import Mock, call

import pytest

from utils.user_information import refresh_information, save_info_from_suite_user


def test_save_info_from_suite_user_refreshes_en_information_once():
    client = Mock()
    client.request.side_effect = [
        {"userHomeBanners": [], "userInformations": []},
        {"informations": [{"id": 1}]},
    ]
    writes = []

    result = save_info_from_suite_user(client, "en", lambda *args: writes.append(args))

    assert result == {"userHomeBanners": [], "userInformations": []}
    assert client.request.call_args_list == [
        call("login_user_info"),
        call("fetch_information"),
    ]
    assert writes == [
        ("userHomeBanners.json", []),
        ("userInformations.json", [{"id": 1}]),
    ]


def test_save_info_from_suite_user_writes_non_en_information():
    client = Mock()
    client.request.return_value = {
        "userHomeBanners": [],
        "userInformations": [{"id": 1}],
    }
    writes = []

    save_info_from_suite_user(client, "jp", lambda *args: writes.append(args))

    assert client.request.call_args_list == [call("login_user_info")]
    assert writes == [
        ("userHomeBanners.json", []),
        ("userInformations.json", [{"id": 1}]),
    ]


def test_refresh_information_preserves_validation_error_semantics():
    client = Mock()
    client.request.return_value = {"informations": "not-a-list"}

    with pytest.raises(RuntimeError, match="Invalid information response"):
        refresh_information(client, Mock())
