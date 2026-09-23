from __future__ import annotations

from typing import Any


def display_status(entry: Any) -> str:
    status_value = getattr(getattr(entry, "status", None), "value", None)
    normalized = str(status_value).strip() or "unknown"
    if getattr(entry, "cancel_requested", False) and normalized == "running":
        return "cancel_requested"
    return normalized
