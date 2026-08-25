from __future__ import annotations

from typing import Any, overload


def normalize_text(value: Any, *, none: str = "") -> str:
    if value is None:
        return none
    return str(value).strip()


def coerce_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def coerce_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


@overload
def safe_int(value: Any, *, default: int = 0) -> int: ...


@overload
def safe_int(value: Any, *, default: None) -> int | None: ...


def safe_int(value: Any, *, default: int | None = 0) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = safe_int(value, default=None)
    return parsed if parsed is not None and parsed > 0 else None


def safe_float(value: Any, *, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_bool(
    value: Any,
    *,
    default: bool = False,
    true_values: frozenset[str] = frozenset({"1", "true", "yes", "y", "on"}),
    false_values: frozenset[str] = frozenset({"0", "false", "no", "n", "off"}),
) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = normalize_text(value).lower()
    if text in true_values:
        return True
    if text in false_values:
        return False
    return default


def coerce_bool(value: Any) -> bool:
    if isinstance(value, (bool, str)) or value is None:
        return normalize_bool(value)
    return bool(value)


def coerce_int_mapping(value: Any, *, default: int = 0) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    payload: dict[str, int] = {}
    for key, raw in value.items():
        name = normalize_text(key)
        if not name:
            continue
        payload[name] = safe_int(raw, default=default)
    return payload


__all__ = [
    "coerce_bool",
    "coerce_int_mapping",
    "coerce_list",
    "coerce_mapping",
    "mapping_or_empty",
    "normalize_bool",
    "normalize_text",
    "positive_int",
    "safe_float",
    "safe_int",
]
