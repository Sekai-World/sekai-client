"""Focused tests for upstream response models and boundary validation (#56).

Covers malformed, partial, and incompatible responses for every validated
surface (auth/login, version_info, system data, current event, ranking
snapshot, information, and master data) plus last-known-good state
preservation in the consumers that overwrite module-level state.
"""

from unittest.mock import Mock

import pytest

import check_update
import event_tracker
from response_models import (
    ResponseValidationError,
    validate_auth_response,
    validate_current_event_response,
    validate_event_ranking_snapshot,
    validate_information,
    validate_master_data,
    validate_system_data,
    validate_version_info,
)

# --------------------------------------------------------------------------- #
# auth/login
# --------------------------------------------------------------------------- #


def _valid_auth(**overrides):
    response = {
        "sessionToken": "session",
        "appVersion": "1.0.0",
        "dataVersion": "1.0.0",
        "assetVersion": "1.0.0",
        "multiPlayVersion": "1.0.0",
    }
    response.update(overrides)
    return response


def test_validate_auth_response_accepts_valid():
    assert validate_auth_response(_valid_auth())["sessionToken"] == "session"


@pytest.mark.parametrize(
    "response",
    [
        None,
        b"data",
        {},
        {"sessionToken": ""},
        _valid_auth(appVersion=123),
        _valid_auth(dataVersion=None),
        _valid_auth(multiPlayVersion=1.5),
        _valid_auth(multiPlayVersion=True),
        _valid_auth(suiteMasterSplitPath="master/a"),
        _valid_auth(suiteMasterSplitPath=[1, 2]),
    ],
)
def test_validate_auth_response_rejects_invalid(response):
    with pytest.raises(ResponseValidationError):
        validate_auth_response(response)


def test_validate_auth_response_reports_field():
    with pytest.raises(ResponseValidationError) as exc:
        validate_auth_response({"sessionToken": "s"})
    assert exc.value.field == "appVersion"
    assert exc.value.source == "auth/login"


def test_validate_auth_response_requires_cdn_version_when_requested():
    with pytest.raises(ResponseValidationError) as exc:
        validate_auth_response(_valid_auth(), require_cdn_version=True)
    assert exc.value.field == "cdnVersion"


def test_validate_auth_response_accepts_cdn_version():
    validate_auth_response(_valid_auth(cdnVersion="20240101"), require_cdn_version=True)


# --------------------------------------------------------------------------- #
# version_info
# --------------------------------------------------------------------------- #


def test_validate_version_info_accepts_valid():
    validate_version_info({"appVersion": "1", "dataVersion": "1", "assetVersion": "1"})


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"appVersion": "1", "dataVersion": "1"},
        {"appVersion": 1, "dataVersion": "1", "assetVersion": "1"},
        {"appVersion": "1", "dataVersion": "1", "assetVersion": "1", "cdnVersion": 1.5},
    ],
)
def test_validate_version_info_rejects_invalid(response):
    with pytest.raises(ResponseValidationError):
        validate_version_info(response)


# --------------------------------------------------------------------------- #
# system data
# --------------------------------------------------------------------------- #


def _valid_system():
    return {
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


def test_validate_system_data_accepts_valid():
    validate_system_data(_valid_system())


def test_validate_system_data_accepts_missing_optional_data_version():
    response = _valid_system()
    del response["appVersions"][0]["dataVersion"]

    validate_system_data(response)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"maintenanceStatus": "none"},
        {"appVersions": []},
        {"maintenanceStatus": 0, "appVersions": []},
        {"maintenanceStatus": "none", "appVersions": [{"appVersion": "1"}]},
        {
            "maintenanceStatus": "none",
            "appVersions": [
                {
                    "appVersion": "1.0.0",
                    "dataVersion": "1.0.0",
                    "assetVersion": "1.0.0",
                    "appVersionStatus": 0,
                }
            ],
        },
    ],
)
def test_validate_system_data_rejects_invalid(response):
    with pytest.raises(ResponseValidationError):
        validate_system_data(response)


# --------------------------------------------------------------------------- #
# current event
# --------------------------------------------------------------------------- #


def _valid_event_json(event_id: int = 12):
    return {
        "id": event_id,
        "eventType": "marathon",
        "startAt": 0,
        "closedAt": 2_000_000,
        "rankingAnnounceAt": 1_100_000,
        "aggregateAt": 1_000_000,
    }


def test_validate_current_event_response_accepts_valid():
    event = validate_current_event_response({"eventJson": _valid_event_json()})
    assert event["id"] == 12


def test_validate_current_event_response_returns_none_for_no_event():
    assert validate_current_event_response({"eventJson": None}) is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"eventJson": "not-an-object"},
        {"eventJson": {}},
        {"eventJson": {"id": "bad"}},
        {"eventJson": {"id": 12, "eventType": 0}},
        {"eventJson": {**_valid_event_json(), "startAt": "soon"}},
        {"eventJson": {**_valid_event_json(), "aggregateAt": None}},
    ],
)
def test_validate_current_event_response_rejects_invalid(payload):
    with pytest.raises(ResponseValidationError):
        validate_current_event_response(payload)


# --------------------------------------------------------------------------- #
# event ranking snapshot
# --------------------------------------------------------------------------- #


def _valid_snapshot():
    return {
        "first100": {
            "isEventAggregate": False,
            "rankings": [{"rank": 1, "score": 1000}],
        },
        "border": {
            "borderRankings": [{"rank": 101, "score": 500}],
        },
    }


def test_validate_event_ranking_snapshot_accepts_valid():
    validate_event_ranking_snapshot(_valid_snapshot())


def _aggregate_snapshot(border=None):
    snapshot = {
        "first100": {
            "isEventAggregate": True,
            "rankings": [{"rank": 1, "score": 1000}],
        },
    }
    if border is not None:
        snapshot["border"] = border
    return snapshot


def test_validate_event_ranking_snapshot_accepts_null_border_for_aggregate():
    # Aggregate (event-final) rankings have no border line, so a missing/null
    # border is allowed.
    validate_event_ranking_snapshot(_aggregate_snapshot())
    validate_event_ranking_snapshot(_aggregate_snapshot(border=None))


def test_validate_event_ranking_snapshot_accepts_aggregate_with_border():
    validate_event_ranking_snapshot(
        _aggregate_snapshot(border={"borderRankings": [{"rank": 101, "score": 500}]})
    )


def test_validate_event_ranking_snapshot_rejects_missing_border_for_non_aggregate():
    # Non-aggregate first100 must carry a border; track_event_scores consumes it.
    with pytest.raises(ResponseValidationError):
        validate_event_ranking_snapshot(
            {
                "first100": {
                    "isEventAggregate": False,
                    "rankings": [{"rank": 1, "score": 1}],
                }
            }
        )
    with pytest.raises(ResponseValidationError):
        validate_event_ranking_snapshot(
            {
                "first100": {
                    "isEventAggregate": False,
                    "rankings": [{"rank": 1, "score": 1}],
                },
                "border": None,
            }
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"first100": None},
        {"first100": {"rankings": []}},
        {"first100": {"isEventAggregate": "no", "rankings": []}},
        {"first100": {"isEventAggregate": False, "rankings": "x"}},
        {"first100": {"isEventAggregate": False, "rankings": [{"rank": 0}]}},
        {"first100": {"isEventAggregate": False, "rankings": [{"score": -1}]}},
        {
            "first100": {"isEventAggregate": False, "rankings": []},
            "border": {"borderRankings": [{"rank": 101}]},
        },
        {
            "first100": {"isEventAggregate": False, "rankings": []},
            "border": {"borderRankings": [{"rank": 101, "score": -5}]},
        },
    ],
)
def test_validate_event_ranking_snapshot_rejects_invalid(snapshot):
    with pytest.raises(ResponseValidationError):
        validate_event_ranking_snapshot(snapshot)


# --------------------------------------------------------------------------- #
# information
# --------------------------------------------------------------------------- #


def test_validate_information_accepts_valid():
    validate_information(
        {"informations": [{"id": 1}], "userHomeBanners": [], "userInformations": []}
    )


@pytest.mark.parametrize(
    "response",
    [
        "not-an-object",
        {"informations": "scalar"},
        {"userHomeBanners": 5},
    ],
)
def test_validate_information_rejects_invalid(response):
    with pytest.raises(ResponseValidationError):
        validate_information(response)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"userHomeBanners": []},
        {"userInformations": []},
        {"informations": "scalar"},
    ],
)
def test_validate_information_requires_informations_list(response):
    """``refresh_information`` unconditionally indexes ``res["informations"]``."""
    with pytest.raises(ResponseValidationError):
        validate_information(response)


# --------------------------------------------------------------------------- #
# master data
# --------------------------------------------------------------------------- #


def test_validate_master_data_accepts_valid():
    validate_master_data(
        {"cards": [{"id": 1}], "events": [{"id": 2}], "other": [{"x": 1}]}
    )


@pytest.mark.parametrize(
    "data",
    [
        {"cards": "not-a-list"},
        {"cards": [{"no_id": 1}]},
        {"cards": [{"id": "string"}]},
        {"events": ["not-an-object"]},
        # A dict-typed top-level table that is NOT the explicitly named singleton
        # is still rejected (the fix must not relax to "any dict").
        {"unknownTable": {"x": 1}},
        {"unknownTable": {"id": 1, "boostQuantity": 2, "recoveryBoostStamina": 3}},
    ],
)
def test_validate_master_data_rejects_invalid(data):
    with pytest.raises(ResponseValidationError):
        validate_master_data(data)


def test_validate_master_data_accepts_mysekai_stamina_recovery_singleton():
    # Required fields present as int; id optional but accepted.
    validate_master_data(
        {
            "cards": [{"id": 1}],
            "mysekaiStaminaRecovery": {
                "id": 1,
                "boostQuantity": 10,
                "recoveryBoostStamina": 20,
            },
        }
    )


def test_validate_master_data_accepts_mysekai_stamina_recovery_without_id():
    # ``id`` is optional; the two required int fields are enough.
    validate_master_data(
        {
            "mysekaiStaminaRecovery": {
                "boostQuantity": 10,
                "recoveryBoostStamina": 20,
            },
        }
    )


@pytest.mark.parametrize(
    "singleton",
    [
        "not-a-dict",  # must be an object, not a list/scalar
        [
            {"id": 1, "boostQuantity": 10, "recoveryBoostStamina": 20}
        ],  # must not be a list
        {"boostQuantity": 10},  # missing recoveryBoostStamina
        {"recoveryBoostStamina": 20},  # missing boostQuantity
        {
            "boostQuantity": True,
            "recoveryBoostStamina": 20,
        },  # bool rejected (int subclass)
        {"boostQuantity": 10, "recoveryBoostStamina": False},  # bool rejected
        {"boostQuantity": "10", "recoveryBoostStamina": 20},  # wrong type
        {
            "boostQuantity": 10,
            "recoveryBoostStamina": 20,
            "id": True,
        },  # id bool rejected
        {"boostQuantity": 10, "recoveryBoostStamina": 20, "id": "1"},  # id wrong type
    ],
)
def test_validate_master_data_rejects_invalid_mysekai_stamina_recovery(singleton):
    with pytest.raises(ResponseValidationError):
        validate_master_data({"mysekaiStaminaRecovery": singleton})


# --------------------------------------------------------------------------- #
# last-known-good state preservation in consumers
# --------------------------------------------------------------------------- #


def test_event_tracker_refresh_version_preserves_event_data_on_bad_payload(monkeypatch):
    good_event = _valid_event_json()
    monkeypatch.setattr(event_tracker, "event_data", good_event)
    monkeypatch.setattr(event_tracker, "_world_blooms_cache", None)

    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"eventJson": "corrupted"}
    monkeypatch.setattr(
        event_tracker._external_session, "get", Mock(return_value=response)
    )
    # version_info request must not be reached
    request = Mock(side_effect=AssertionError("version_info should not be requested"))
    monkeypatch.setattr(event_tracker.jsonrpc_client, "request", request)

    with pytest.raises(RuntimeError):
        event_tracker.refresh_version()

    # last-known-good event_data must be untouched
    assert event_tracker.event_data == good_event


def test_event_tracker_track_event_scores_preserves_state_on_bad_snapshot(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        event_tracker,
        "event_data",
        _valid_event_json(),
    )
    outbox = Mock()
    monkeypatch.setattr(event_tracker, "ranking_outbox", outbox)
    monkeypatch.setattr(
        event_tracker.jsonrpc_client,
        "request",
        Mock(
            return_value={
                "first100": {"isEventAggregate": False, "rankings": [{"rank": -1}]},
                "border": {"borderRankings": []},
            }
        ),
    )

    with pytest.raises(RuntimeError, match="Invalid event ranking snapshot"):
        event_tracker.track_event_scores(1_000)

    # no enqueue: last-known-good delivery state is preserved
    outbox.enqueue.assert_not_called()


# --------------------------------------------------------------------------- #
# cdnVersion type/required consistency (auth + version data)
# --------------------------------------------------------------------------- #


def test_validate_version_info_requires_cdn_version_for_region():
    base = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}
    # missing cdnVersion when required -> rejected
    with pytest.raises(ResponseValidationError):
        validate_version_info(base, require_cdn_version=True)
    # bool/float cdnVersion -> rejected (even when present/required)
    for bad in (True, 1.5, False):
        with pytest.raises(ResponseValidationError):
            validate_version_info({**base, "cdnVersion": bad}, require_cdn_version=True)
    # valid str/int cdnVersion when required -> accepted
    validate_version_info({**base, "cdnVersion": "20240101"}, require_cdn_version=True)
    validate_version_info({**base, "cdnVersion": 20240101}, require_cdn_version=True)


def test_validate_version_info_rejects_bool_float_optional_cdn_version():
    base = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}
    for bad in (True, 1.5, False):
        with pytest.raises(ResponseValidationError):
            validate_version_info({**base, "cdnVersion": bad})
        with pytest.raises(ResponseValidationError):
            validate_version_info({**base, "appHash": bad, "assetHash": bad})


def test_validate_version_info_requires_non_empty_app_hash_for_jp_en():
    base = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}
    # missing appHash when required -> rejected
    with pytest.raises(ResponseValidationError):
        validate_version_info(base, require_app_hash=True)
    # empty-string appHash when required -> rejected
    with pytest.raises(ResponseValidationError):
        validate_version_info({**base, "appHash": ""}, require_app_hash=True)
    # non-str appHash when required -> rejected
    with pytest.raises(ResponseValidationError):
        validate_version_info({**base, "appHash": 123}, require_app_hash=True)
    # valid non-empty appHash when required -> accepted
    validate_version_info({**base, "appHash": "deadbeef"}, require_app_hash=True)
    # appHash is not required when the flag is not set -> missing accepted
    validate_version_info(base)


def test_validate_auth_response_requires_cdn_version_type_for_region():
    base = _valid_auth()
    # missing cdnVersion when required -> rejected
    with pytest.raises(ResponseValidationError):
        validate_auth_response(base, require_cdn_version=True)
    # bool/float cdnVersion -> rejected
    for bad in (True, 1.5, False):
        with pytest.raises(ResponseValidationError):
            validate_auth_response(
                {**base, "cdnVersion": bad}, require_cdn_version=True
            )
    # assetHash bool/float -> rejected (optional-present path)
    for bad in (True, 1.5):
        with pytest.raises(ResponseValidationError):
            validate_auth_response({**base, "assetHash": bad})
    # valid str/int cdnVersion when required -> accepted
    validate_auth_response({**base, "cdnVersion": "20240101"}, require_cdn_version=True)


# --------------------------------------------------------------------------- #
# current-event timing monotonicity / non-negativity
# --------------------------------------------------------------------------- #


def _event_with_timing(start, aggregate, ranking_announce, closed):
    event = _valid_event_json()
    event.update(
        {
            "startAt": start,
            "aggregateAt": aggregate,
            "rankingAnnounceAt": ranking_announce,
            "closedAt": closed,
        }
    )
    return {"eventJson": event}


@pytest.mark.parametrize(
    "timing",
    [
        (-1, 1_000_000, 1_100_000, 2_000_000),  # negative startAt
        (0, -5, 1_100_000, 2_000_000),  # negative aggregateAt
        (0, 1_000_000, 1_100_000, -2),  # negative closedAt
        (2_000_000, 1_000_000, 1_100_000, 2_000_000),  # startAt > aggregateAt
        (0, 2_000_000, 1_100_000, 2_000_000),  # aggregateAt > rankingAnnounceAt
        (0, 1_000_000, 2_000_000, 1_100_000),  # rankingAnnounceAt > closedAt
    ],
)
def test_validate_current_event_response_rejects_bad_timing(timing):
    with pytest.raises(ResponseValidationError):
        validate_current_event_response(_event_with_timing(*timing))


def test_validate_current_event_response_accepts_monotonic_timing():
    event = validate_current_event_response(
        _event_with_timing(0, 1_000_000, 1_100_000, 2_000_000)
    )
    assert event["startAt"] == 0
    assert event["closedAt"] == 2_000_000


# --------------------------------------------------------------------------- #
# refresh_version validates explicit candidate before writing versions.json
# --------------------------------------------------------------------------- #


def test_check_update_refresh_version_rejects_bad_explicit_candidate(monkeypatch):
    monkeypatch.setattr(check_update, "pjsk_region", "jp")
    written = []
    monkeypatch.setattr(
        check_update, "_write_master_file", lambda *args: written.append(args)
    )
    monkeypatch.setattr(
        check_update, "_fetch_master_data_by_region", lambda *a, **k: {}
    )

    bad_candidate = {"dataVersion": "1", "assetVersion": "1"}  # missing appVersion
    with pytest.raises(RuntimeError):
        check_update.refresh_version(candidate=bad_candidate)

    # versions.json must NOT be written for a malformed candidate
    assert all(name != "versions.json" for name, _ in written)


def test_check_update_refresh_version_requires_cdn_version_for_cn_candidate(
    monkeypatch,
):
    monkeypatch.setattr(check_update, "pjsk_region", "cn")
    written = []
    monkeypatch.setattr(
        check_update, "_write_master_file", lambda *args: written.append(args)
    )
    monkeypatch.setattr(
        check_update, "_fetch_master_data_by_region", lambda *a, **k: {}
    )

    # cn candidate missing cdnVersion must be rejected before any write
    bad_candidate = {
        "appVersion": "1",
        "dataVersion": "1",
        "assetVersion": "1",
    }
    with pytest.raises(RuntimeError):
        check_update.refresh_version(candidate=bad_candidate)

    assert all(name != "versions.json" for name, _ in written)


# --------------------------------------------------------------------------- #
# event_tracker.refresh_version validates both before assigning state
# --------------------------------------------------------------------------- #


def _patch_event_tracker_sources(monkeypatch, current_event_payload, version_info):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = current_event_payload
    monkeypatch.setattr(
        event_tracker._external_session, "get", Mock(return_value=response)
    )
    monkeypatch.setattr(
        event_tracker.jsonrpc_client,
        "request",
        Mock(return_value=version_info),
    )


def test_event_tracker_refresh_version_preserves_all_state_on_bad_version_info(
    monkeypatch,
):
    good_event = _valid_event_json()
    good_version = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}
    monkeypatch.setattr(event_tracker, "event_data", good_event)
    monkeypatch.setattr(event_tracker, "version_info", good_version)
    monkeypatch.setattr(event_tracker, "_world_blooms_cache", ("sentinel", None))

    _patch_event_tracker_sources(
        monkeypatch,
        {"eventJson": _valid_event_json()},
        {"appVersion": "1"},  # malformed version_info (missing dataVersion)
    )

    with pytest.raises(RuntimeError):
        event_tracker.refresh_version()

    # neither event_data, version_info, nor _world_blooms_cache is overwritten
    assert event_tracker.event_data == good_event
    assert event_tracker.version_info == good_version
    assert event_tracker._world_blooms_cache == ("sentinel", None)


def test_event_tracker_refresh_version_preserves_all_state_on_bad_current_event(
    monkeypatch,
):
    good_event = _valid_event_json()
    good_version = {"appVersion": "1", "dataVersion": "1", "assetVersion": "1"}
    monkeypatch.setattr(event_tracker, "event_data", good_event)
    monkeypatch.setattr(event_tracker, "version_info", good_version)
    monkeypatch.setattr(event_tracker, "_world_blooms_cache", ("sentinel", None))

    _patch_event_tracker_sources(
        monkeypatch,
        {"eventJson": "corrupted"},  # malformed current-event
        good_version,
    )

    with pytest.raises(RuntimeError):
        event_tracker.refresh_version()

    assert event_tracker.event_data == good_event
    assert event_tracker.version_info == good_version
    assert event_tracker._world_blooms_cache == ("sentinel", None)


# --------------------------------------------------------------------------- #
# system_data nested cdnVersion type consistency
# --------------------------------------------------------------------------- #


def test_validate_system_data_rejects_bool_float_nested_cdn_version():
    base = _valid_system()
    for bad in (True, 1.5, False):
        with pytest.raises(ResponseValidationError):
            validate_system_data(
                {**base, "appVersions": [{**base["appVersions"][0], "cdnVersion": bad}]}
            )
    # valid str/int nested cdnVersion accepted
    validate_system_data(
        {**base, "appVersions": [{**base["appVersions"][0], "cdnVersion": "20240101"}]}
    )
    validate_system_data(
        {**base, "appVersions": [{**base["appVersions"][0], "cdnVersion": 20240101}]}
    )


# --------------------------------------------------------------------------- #
# current-event id must be a positive integer (no bool/zero/negative)
# --------------------------------------------------------------------------- #


def test_validate_current_event_response_rejects_non_positive_id():
    for bad_id in (0, -1, True, False):
        with pytest.raises(ResponseValidationError):
            validate_current_event_response({"eventJson": _valid_event_json(bad_id)})
    # positive int accepted
    assert validate_current_event_response({"eventJson": _valid_event_json(100)})


def test_validate_current_event_response_rejects_non_int_id():
    with pytest.raises(ResponseValidationError):
        validate_current_event_response(
            {"eventJson": {**_valid_event_json(), "id": "100"}}
        )


# --------------------------------------------------------------------------- #
# current-event optional region/regionCode compatibility
# --------------------------------------------------------------------------- #


def _event_with_region(region_value, *, key="region"):
    event = _valid_event_json()
    event[key] = region_value
    return {"eventJson": event}


def test_validate_current_event_response_optional_region_valid_when_present():
    # present non-empty str passes; missing region field is never required
    assert validate_current_event_response(_event_with_region("jp"))
    assert validate_current_event_response(_event_with_region("jp", key="regionCode"))
    assert validate_current_event_response({"eventJson": _valid_event_json()})


def test_validate_current_event_response_optional_region_rejects_bad_type():
    for bad in (True, 1, "", 0):
        with pytest.raises(ResponseValidationError):
            validate_current_event_response(_event_with_region(bad))
        with pytest.raises(ResponseValidationError):
            validate_current_event_response(_event_with_region(bad, key="regionCode"))


def test_validate_current_event_response_optional_region_compares_when_expected():
    # matching region passes
    assert validate_current_event_response(
        _event_with_region("jp"), expected_region="jp"
    )
    assert validate_current_event_response(
        _event_with_region("JP", key="regionCode"), expected_region="jp"
    )
    # mismatch raises; missing region field does NOT raise (optional)
    with pytest.raises(ResponseValidationError):
        validate_current_event_response(_event_with_region("en"), expected_region="jp")
    assert validate_current_event_response(
        {"eventJson": _valid_event_json()}, expected_region="jp"
    )
