from __future__ import annotations

from typing import Any, overload


def normalize_text(value: Any, *, none: str = "") -> str:
    if value is None:
        return none
    return str(value).strip()


def copy_dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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


_TRUE_TEXTS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_TEXTS = frozenset({"0", "false", "no", "n", "off"})


def normalize_bool(
    value: Any,
    *,
    default: bool = False,
) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = normalize_text(value).lower()
    if text in _TRUE_TEXTS:
        return True
    if text in _FALSE_TEXTS:
        return False
    return default


__all__ = [
    "copy_dict_or_empty",
    "normalize_bool",
    "normalize_text",
    "positive_int",
    "safe_float",
    "safe_int",
]
