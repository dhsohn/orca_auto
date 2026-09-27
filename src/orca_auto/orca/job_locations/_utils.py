from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import normalize_text


def normalize_path_text(value: Any) -> str:
    raw = normalize_text(value)
    if not raw:
        return ""
    try:
        candidate = Path(raw).expanduser()
    except OSError:
        return raw
    try:
        return str(candidate.resolve())
    except OSError:
        return str(candidate)


def resource_dict_from_any(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw in value.items():
        key_text = normalize_text(key)
        if not key_text:
            continue
        try:
            result[key_text] = int(raw)
        except (TypeError, ValueError):
            continue
    return result


__all__ = [
    "normalize_path_text",
    "resource_dict_from_any",
]
