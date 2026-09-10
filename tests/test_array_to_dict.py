import pytest

from utils.array_to_dict import restore_compact_data


def test_restore_compact_data_handles_empty_and_enum_only_data():
    assert restore_compact_data({}) == []
    assert restore_compact_data({"__ENUM__": {"status": ["ready"]}}) == []


def test_restore_compact_data_restores_enum_indexes_and_preserves_none():
    data = {
        "id": [1, 2],
        "status": [0, None],
        "__ENUM__": {"status": ["ready", "done"]},
    }

    assert restore_compact_data(data) == [
        {"id": 1, "status": "ready"},
        {"id": 2, "status": None},
    ]


def test_restore_compact_data_rejects_uneven_columns():
    with pytest.raises(ValueError, match="equal lengths"):
        restore_compact_data({"id": [1, 2], "name": ["one"]})


def test_restore_compact_data_rejects_malformed_enum_values():
    with pytest.raises(TypeError, match="enum values"):
        restore_compact_data({"__ENUM__": {"status": "ready"}})


@pytest.mark.parametrize("enum_index", ["0", 1.0, True, -1, 1])
def test_restore_compact_data_rejects_malformed_enum_indexes(enum_index):
    """Non-int enum indexes (str/float/bool/out-of-range) are rejected."""
    data = {
        "status": [enum_index],
        "__ENUM__": {"status": ["ready"]},
    }

    with pytest.raises(ValueError, match="enum index"):
        restore_compact_data(data)


# --------------------------------------------------------------------------- #
# nuverse positional schema overlay
# --------------------------------------------------------------------------- #


def test_get_structures_applies_nuverse_overlay_without_app_ver():
    """Overlay applies even when no app version is given."""
    from nuverse_positional_structures import NUVERSE_POSITIONAL_STRUCTURES
    from utils.array_to_dict import get_structures_for_app_ver

    structures = get_structures_for_app_ver(None)

    # BASE_STRUCTURES lags behind the bundle for these tables.
    assert structures["cardCostume3ds"] == [
        "cardId",
        "costume3dId",
        "isInitialObtainHair",
    ]
    assert structures["cards"] == NUVERSE_POSITIONAL_STRUCTURES["cards"]


def test_get_structures_overlay_wins_over_compatibility_entries():
    """Bundle layouts win over the compatibility entries."""
    from nuverse_positional_structures import NUVERSE_POSITIONAL_STRUCTURES
    from utils.array_to_dict import get_structures_for_app_ver

    structures = get_structures_for_app_ver("6.0.0")

    assert structures["cards"] == NUVERSE_POSITIONAL_STRUCTURES["cards"]
    assert structures["virtualLives"] == NUVERSE_POSITIONAL_STRUCTURES["virtualLives"]


def test_apply_nuverse_overlay_preserves_tables_absent_from_bundle():
    """Tables absent from the bundle keep their existing spec."""
    from utils.array_to_dict_structures import apply_nuverse_overlay

    result = {"handMaintainedTable": ["id", "name"]}
    apply_nuverse_overlay(result)

    assert result["handMaintainedTable"] == ["id", "name"]
    assert result["cardCostume3ds"] == ["cardId", "costume3dId", "isInitialObtainHair"]


def test_overlay_spec_converts_real_positional_record():
    """The overlay spec decodes a real positional record."""
    from utils.array_to_dict import convert_array_to_dict, get_structures_for_app_ver

    structures = get_structures_for_app_ver()

    out = convert_array_to_dict(
        [4, 29001, False], structures["cardCostume3ds"], structure_name="cardCostume3ds"
    )
    assert out == {"cardId": 4, "costume3dId": 29001, "isInitialObtainHair": False}


def test_overlay_spec_converts_flat_struct_column():
    """A flat-struct column decodes via its tuple mapping."""
    from utils.array_to_dict import convert_array_to_dict, get_structures_for_app_ver

    structures = get_structures_for_app_ver()
    row = [None] * len(structures["gachas"])
    row[19] = [145, "summary", "desc", "bubble", "text"]

    out = convert_array_to_dict(row, structures["gachas"], structure_name="gachas")

    assert out["gachaInformation"] == {
        "gachaId": 145,
        "summary": "summary",
        "description": "desc",
        "bubbleAssetbundleName": "bubble",
        "bubbleText": "text",
    }
