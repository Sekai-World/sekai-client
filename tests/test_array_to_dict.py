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
    data = {
        "status": [enum_index],
        "__ENUM__": {"status": ["ready"]},
    }

    with pytest.raises(ValueError, match="enum index"):
        restore_compact_data(data)
