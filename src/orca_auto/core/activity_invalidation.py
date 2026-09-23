"""Durable invalidation of the disposable activity read model.

State writers call this inside their state mutation lock, BEFORE publication.
Readers capture tickets, read under that same state lock, commit the projection,
then acknowledge only unchanged tickets. No SQLite lock enters this lock order.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .utils.lock import file_lock
from .utils.persistence import atomic_write_json

DIR_NAME = ".activity-dirty"


def invalidate_state(path: Path, *, root: Path | None = None) -> None:
    resolved = path.resolve()
    roots = (root.resolve(),) if root is not None else (resolved, *resolved.parents)
    for candidate in roots:
        directory = candidate / DIR_NAME
        if not directory.is_dir():
            continue
        key = hashlib.sha256(str(resolved).encode()).hexdigest()
        with file_lock(directory / "journal.lock"):
            atomic_write_json(
                directory / f"{key}.json",
                {"path": str(resolved), "ticket": uuid4().hex},
            )


def capture(root: Path) -> dict[str, tuple[Path, str]]:
    directory = root / DIR_NAME
    with file_lock(directory / "journal.lock"):
        tickets = {}
        for marker in directory.glob("*.json"):
            value = json.loads(marker.read_text())
            tickets[str(value["path"])] = (marker, str(value["ticket"]))
        return tickets


def acknowledge(root: Path, tickets: dict[str, tuple[Path, str]]) -> None:
    with file_lock(root / DIR_NAME / "journal.lock"):
        for marker, ticket in tickets.values():
            try:
                current = json.loads(marker.read_text())
            except FileNotFoundError:
                continue
            if current["ticket"] == ticket:
                marker.unlink()
