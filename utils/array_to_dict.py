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

    result = {}

    for i, key in enumerate(key_structure):
        if isinstance(key, str):
            # if key is string, then assign the value to the key
            if i >= len(array_data):
                continue
            if array_data[i] is not None:
                result[key] = array_data[i]
        elif isinstance(key, list):
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
            if i >= len(array_data):
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
            if isinstance(key[1], list):
                # if key is list and the second element is list, then it is a nested list
                if array_data[i] is None:
                    result[key[0]] = []
                    continue
                if not isinstance(array_data[i], list):
                    raise _build_context_error(
                        TypeError,
                        "expected nested list data",
                        structure_name=structure_name,
                        node_path=f"{node_path}.{key[0]}",
                        index=i,
                        key=key,
                        array_data=array_data,
                        key_structure=key_structure,
                    )
                nested_result = []
                for sub_i, sub_array in enumerate(array_data[i]):
                    if sub_array is None:
                        continue
                    nested_result.append(
                        convert_array_to_dict(
                            sub_array,
                            key[1],
                            structure_name=structure_name,
                            node_path=f"{node_path}.{key[0]}[{sub_i}]",
                        )
                    )
                result[key[0]] = nested_result
            elif isinstance(key[1], tuple):
                # if key is list and the second element is tuple, then it is a dict
                if array_data[i] is None:
                    continue
                if not isinstance(array_data[i], list):
                    raise _build_context_error(
                        TypeError,
                        "expected list data for tuple key mapping",
                        structure_name=structure_name,
                        node_path=f"{node_path}.{key[0]}",
                        index=i,
                        key=key,
                        array_data=array_data,
                        key_structure=key_structure,
                    )
                result[key[0]] = {
                    key[1][i]: v for i, v in enumerate(array_data[i]) if v is not None
                }

    return result


def restore_compact_data(data: dict) -> list:
    """
    Original Author: TWY
    convert compact data to original data structure
    :param data: dict
    :return: result: list
    """
    enum = data.get("__ENUM__", {})
    column_labels = []
    columns = []
    for column in data:
        if column == "__ENUM__":
            continue
        column_labels.append(column)
        if column in enum:
            columns.append(
                [(None if i is None else enum[column][i]) for i in data[column]]
            )
        else:
            columns.append(data[column])
    num_entries = min(len(column) for column in columns)
    result = []
    for i in range(num_entries):
        result.append({key: column[i] for key, column in zip(column_labels, columns)})
    return result
