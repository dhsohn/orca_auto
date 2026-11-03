"""The binding's one boundary for reading source bytes and for its byte budgets.

Every stage that binds or verifies file bytes reads them here, with
:func:`capture`, which returns the exact bytes read together with their
identity (:class:`CapturedFile`); the stage then uses those bytes, never a
second read of the path. Every byte-budget check goes through
:func:`input_limit`, :func:`aggregate_limit`, :func:`charge_aggregate` or
:func:`fits_aggregate`, which read the limits from this module when called.
The stable reader and both limits therefore live in one module, so a test that
injects a read failure or shrinks a budget patches this module alone, whatever
stage reads the file.

A new verification boundary captures again: a capture proves what was read at
that moment, and a later stage that must re-check the file reads it afresh.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.confined_io import (
    MAX_AGGREGATE_SNAPSHOT_BYTES,
    MAX_INPUT_SNAPSHOT_BYTES,
    read_stable_regular_file,
)

# The aggregate budget across one generation's inputs, the rewritten bound
# input included.
MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES = MAX_AGGREGATE_SNAPSHOT_BYTES

AGGREGATE_BUDGET_ERROR = "ORCA submission inputs exceed the aggregate snapshot size limit"


def input_limit() -> int:
    """The byte limit of one source input."""
    return MAX_INPUT_SNAPSHOT_BYTES


def aggregate_limit() -> int:
    """The byte limit of all of one generation's inputs together."""
    return MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES


@dataclass(frozen=True)
class CapturedFile:
    """The bytes one stable read returned, and the identity of exactly those bytes."""

    path: Path
    payload: bytes = field(repr=False)

    @property
    def size_bytes(self) -> int:
        return len(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def identity(self) -> dict[str, Any]:
        """``{path, sha256, size_bytes}`` of the captured bytes."""
        return {"path": str(self.path), "sha256": self.sha256, "size_bytes": self.size_bytes}


def capture(
    path: Path,
    *,
    max_bytes: int | None = None,
    require_single_link: bool = False,
) -> CapturedFile:
    """Read ``path`` once through the stable reader; ``max_bytes`` defaults to :func:`input_limit`.

    Raises whatever the stable reader raises: a path that is not a regular
    file, one that changed while it was read, or one over the limit.
    """
    payload = read_stable_regular_file(
        path,
        max_bytes=input_limit() if max_bytes is None else max_bytes,
        require_single_link=require_single_link,
    )
    return CapturedFile(path=path, payload=payload)


def fits_aggregate(consumed_bytes: int, size_bytes: int) -> bool:
    """Whether ``size_bytes`` more stays within the aggregate budget."""
    return consumed_bytes + size_bytes <= aggregate_limit()


def charge_aggregate(consumed_bytes: int, size_bytes: int) -> int:
    """The new running total, or ``ValueError`` when it would exceed the aggregate budget."""
    if not fits_aggregate(consumed_bytes, size_bytes):
        raise ValueError(AGGREGATE_BUDGET_ERROR)
    return consumed_bytes + size_bytes


__all__ = [
    "AGGREGATE_BUDGET_ERROR",
    "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES",
    "CapturedFile",
    "aggregate_limit",
    "capture",
    "charge_aggregate",
    "fits_aggregate",
    "input_limit",
]
