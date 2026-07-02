from __future__ import annotations

from typing import Any

from ..child.process import status_matches
from ..types import QueueStatus


def entry_status_is_running(entry: Any) -> bool:
    return status_matches(getattr(entry, "status", None), QueueStatus.RUNNING)


__all__ = ["entry_status_is_running"]
