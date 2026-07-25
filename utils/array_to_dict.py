from .array_to_dict_structures import (
    get_structures_for_app_ver,
    resolve_structure_compatibility_version,
    structures,
)

__all__ = [
    "structures",
    "get_structures_for_app_ver",
    "resolve_structure_compatibility_version",
    "convert_array_to_dict",
    "restore_compact_data",
]


def _format_debug_value(value, max_len: int = 180) -> str:
    text = repr(value)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _build_context_error(
    error_type,
    message: str,
    *,
    structure_name: str | None,
    node_path: str,
    index: int | None,
    key,
    array_data,
    key_structure,
):
    details = [message]
    details.append(f"structure={structure_name or 'unknown'}")
    details.append(f"path={node_path}")
    if index is not None:
        details.append(f"index={index}")
    details.append(f"key={_format_debug_value(key)}")
    try:
        details.append(f"array_len={len(array_data)}")
    except TypeError:
        details.append("array_len=N/A")
    details.append(f"array_data={repr(array_data)}")
    details.append(f"key_structure={repr(key_structure)}")
    return error_type(" | ".join(details))


def _set_scalar_key(result: dict, key: str, value) -> None:
    if value is not None:
        result[key] = value


def _set_tuple_key(
    result: dict,
    key_name: str,
    tuple_keys: tuple,
    values: list,
) -> None:
    result[key_name] = {
        tuple_keys[idx]: item for idx, item in enumerate(values) if item is not None
    }


def _set_nested_list_key(
    result: dict,
    key_name: str,
    nested_structure: list,
    nested_data: list,
    *,
    structure_name: str | None,
    node_path: str,
) -> None:
    nested_result = []
    for sub_i, sub_array in enumerate(nested_data):
        if sub_array is None:
            continue
        nested_result.append(
            convert_array_to_dict(
                sub_array,
                nested_structure,
                structure_name=structure_name,
                node_path=f"{node_path}.{key_name}[{sub_i}]",
            )
        )
    result[key_name] = nested_result


def _process_structured_key(
    result: dict,
    key,
    current_value,
    *,
    i: int,
    structure_name: str | None,
    node_path: str,
    array_data,
    key_structure,
) -> None:
    if not isinstance(key, list):
        return

    if len(key) < 2:
        raise _build_context_error(
            ValueError,
            "invalid key_structure item, expected [name, sub_structure]",
            structure_name=structure_name,
            node_path=node_path,
            index=i,
            key=key,
            array_data=array_data,
            key_structure=key_structure,
        )

    key_name = key[0]
    key_value = key[1]
    if isinstance(key_value, list):
        if current_value is None:
            result[key_name] = []
            return
        if not isinstance(current_value, list):
            raise _build_context_error(
                TypeError,
                "expected nested list data",
                structure_name=structure_name,
                node_path=f"{node_path}.{key_name}",
                index=i,
                key=key,
                array_data=array_data,
                key_structure=key_structure,
            )
        _set_nested_list_key(
            result,
            key_name,
            key_value,
            current_value,
            structure_name=structure_name,
            node_path=node_path,
        )
        return

    if isinstance(key_value, tuple):
        if current_value is None:
            return
        if not isinstance(current_value, list):
            raise _build_context_error(
                TypeError,
                "expected list data for tuple key mapping",
                structure_name=structure_name,
                node_path=f"{node_path}.{key_name}",
                index=i,
                key=key,
                array_data=array_data,
                key_structure=key_structure,
            )
        _set_tuple_key(result, key_name, key_value, current_value)


def convert_array_to_dict(
    array_data: list,
    key_structure: list,
    *,
    structure_name: str | None = None,
    node_path: str = "root",
) -> dict:
    """
    convert array to dict with given structure
    :param array_data: array data
    :param key_structure: json structure of the result dict
    :return: result dict
    """
    if not isinstance(array_data, list):
        raise _build_context_error(
            TypeError,
            "array_data is not a list",
            structure_name=structure_name,
            node_path=node_path,
            index=None,
            key=None,
            array_data=array_data,
            key_structure=key_structure,
        )

    result: dict = {}

    for i, key in enumerate(key_structure):
        if i >= len(array_data):
            if isinstance(key, list):
                raise _build_context_error(
                    IndexError,
                    "missing array element for structured key",
                    structure_name=structure_name,
                    node_path=f"{node_path}.{key[0]}",
                    index=i,
                    key=key,
                    array_data=array_data,
                    key_structure=key_structure,
                )
            continue

        if isinstance(key, str):
            _set_scalar_key(result, key, array_data[i])
            continue
        _process_structured_key(
            result,
            key,
            array_data[i],
            i=i,
            structure_name=structure_name,
            node_path=node_path,
            array_data=array_data,
            key_structure=key_structure,
        )

    return result


def restore_compact_data(data: dict) -> list[dict]:
    """
    Original Author: TWY
    convert compact data to original data structure
    :param data: dict
    :return: result: list
    """
    if not isinstance(data, dict):
        raise TypeError("compact data must be a dictionary")

    enum = data.get("__ENUM__", {})
    if not isinstance(enum, dict):
        raise TypeError("compact data __ENUM__ must be a dictionary")

    for column, values in enum.items():
        if not isinstance(values, list):
            raise TypeError(f"enum values for column {column!r} must be a list")

    column_labels = [column for column in data if column != "__ENUM__"]
    if not column_labels:
        return []

    columns = {
        column: _restore_compact_column(column, data[column], enum)
        for column in column_labels
    }
    expected_length = len(columns[column_labels[0]])
    for column in column_labels[1:]:
        if len(columns[column]) != expected_length:
            raise ValueError(
                "compact columns must have equal lengths "
                f"(column {column!r} has {len(columns[column])}, "
                f"expected {expected_length})"
            )

    return [
        {column: columns[column][row_index] for column in column_labels}
        for row_index in range(expected_length or 0)
    ]


def _restore_compact_column(column: str, values, enum: dict) -> list:
    if not isinstance(values, list):
        raise TypeError(f"compact column {column!r} must be a list")
    if column not in enum:
        return values

    enum_values = enum[column]
    restored_values: list = []
    for row_index, enum_index in enumerate(values):
        if enum_index is None:
            restored_values.append(None)
            continue
        if isinstance(enum_index, bool) or not isinstance(enum_index, int):
            raise ValueError(
                f"invalid enum index for column {column!r} at row "
                f"{row_index}: {enum_index!r}"
            )
        if enum_index < 0 or enum_index >= len(enum_values):
            raise ValueError(
                f"enum index out of range for column {column!r} at row "
                f"{row_index}: {enum_index}"
            )
        restored_values.append(enum_values[enum_index])
    return restored_values
