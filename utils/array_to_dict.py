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


def convert_array_to_dict(array_data: list, key_structure: list) -> dict:
    """
    convert array to dict with given structure
    :param array_data: array data
    :param key_structure: json structure of the result dict
    :return: result dict
    """
    result = {}

    for i, key in enumerate(key_structure):
        if isinstance(key, str):
            # if key is string, then assign the value to the key
            if i >= len(array_data):
                continue
            if array_data[i] is not None:
                result[key] = array_data[i]
        elif isinstance(key, list):
            if isinstance(key[1], list):
                # if key is list and the second element is list, then it is a nested list
                result[key[0]] = [
                    convert_array_to_dict(sub_array, key[1])
                    for sub_array in array_data[i]
                    if sub_array is not None
                ]
            elif isinstance(key[1], tuple):
                # if key is list and the second element is tuple, then it is a dict
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
