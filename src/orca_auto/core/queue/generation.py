from __future__ import annotations

import re
import secrets
from datetime import datetime

VISIBLE_GENERATION_NAME_RE = re.compile(r"\A\d{8}-\d{6}-[0-9a-f]{8}\Z", re.ASCII)


def is_visible_generation_name(value: str) -> bool:
    """Return whether *value* is an exact user-visible execution generation name."""

    return bool(VISIBLE_GENERATION_NAME_RE.fullmatch(str(value)))


def new_visible_generation_name() -> str:
    """Mint a fresh user-visible generation name (`YYYYMMDD-HHMMSS-<8hex>`).

    Every ORCA execution generation matches VISIBLE_GENERATION_NAME_RE.
    """

    local_timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{local_timestamp}-{secrets.token_hex(4)}"


__all__ = [
    "VISIBLE_GENERATION_NAME_RE",
    "is_visible_generation_name",
    "new_visible_generation_name",
]
