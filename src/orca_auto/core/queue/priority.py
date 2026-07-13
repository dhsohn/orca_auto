from __future__ import annotations

from typing import Any


def normalize_queue_priority(value: Any, *, default: int = 10) -> int:
    """Return an integer queue priority without treating zero as missing."""
    if value is None:
        return int(default)
    if isinstance(value, bool):
        raise ValueError("queue priority must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return int(default)
        try:
            return int(normalized)
        except ValueError as exc:
            raise ValueError("queue priority must be an integer") from exc
    raise ValueError("queue priority must be an integer")


__all__ = ["normalize_queue_priority"]
