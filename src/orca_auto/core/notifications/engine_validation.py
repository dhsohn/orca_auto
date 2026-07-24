from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def _required_value(values: Mapping[str, object], field_name: str) -> object:
    if field_name not in values:
        raise KeyError(field_name)
    return values[field_name]


def _required_str(values: Mapping[str, object], field_name: str) -> str:
    value = _required_value(values, field_name)
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    return value


def _required_path(values: Mapping[str, object], field_name: str) -> Path:
    value = _required_value(values, field_name)
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be pathlib.Path")
    return value


def _required_int(values: Mapping[str, object], field_name: str) -> int:
    value = _required_value(values, field_name)
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be int")
    return value


def _optional_int_dict(
    values: Mapping[str, object],
    field_name: str,
) -> dict[str, int] | None:
    value = values.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be dict[str, int] or None")
    if not all(isinstance(key, str) and isinstance(item, int) for key, item in value.items()):
        raise TypeError(f"{field_name} must be dict[str, int] or None")
    return dict(value)


__all__ = [
    "_optional_int_dict",
    "_required_int",
    "_required_path",
    "_required_str",
    "_required_value",
]
