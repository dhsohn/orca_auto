from __future__ import annotations

from typing import Any

from orca_auto.core.utils.coercion import (
    normalize_text as normalize_text,
)
from orca_auto.core.utils.coercion import (
    safe_int as _shared_safe_int,
)


def safe_int(value: Any) -> int | None:
    return _shared_safe_int(value, default=None)


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = safe_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed
