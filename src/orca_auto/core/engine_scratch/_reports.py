"""Read-only report dataclasses returned to operators and the CLI:
``PublicationJournalStatus`` describes an interrupted publication journal,
``ScratchWorkspaceReport`` classifies one entry under the scratch root, and
``ScratchWorkspaceRemoval`` records what an operator removal cleaned up.
They carry no behaviour beyond ``as_payload`` serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PublicationJournalStatus:
    """What an interrupted publication left in a durable generation directory."""

    path: Path
    phase: str | None
    item_count: int
    corrupt: bool
    detail: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "phase": self.phase,
            "item_count": self.item_count,
            "corrupt": self.corrupt,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScratchWorkspaceReport:
    """Read-only classification of one entry under the scratch root.

    ``blocks_launch`` mirrors the sweep inside ``EngineScratchWorkspace.create``:
    it is true exactly when that sweep would raise for this entry, and
    ``detail`` is the message it would raise with.
    """

    path: Path
    name: str
    state: str
    manifest_valid: bool
    owner_pid: int | None
    owner_process_start_ticks: int | None
    owner_boot_id: str | None
    durable_dir: str | None
    max_task_memory_bytes: int | None
    size_bytes: int
    size_walk_truncated: bool
    blocks_launch: bool
    detail: str | None
    publication_journal: PublicationJournalStatus | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.name,
            "state": self.state,
            "manifest_valid": self.manifest_valid,
            "owner_pid": self.owner_pid,
            "owner_process_start_ticks": self.owner_process_start_ticks,
            "owner_boot_id": self.owner_boot_id,
            "durable_dir": self.durable_dir,
            "max_task_memory_bytes": self.max_task_memory_bytes,
            "size_bytes": self.size_bytes,
            "size_walk_truncated": self.size_walk_truncated,
            "blocks_launch": self.blocks_launch,
            "detail": self.detail,
            "publication_journal": (
                None if self.publication_journal is None else self.publication_journal.as_payload()
            ),
        }


@dataclass(frozen=True)
class ScratchWorkspaceRemoval:
    report: ScratchWorkspaceReport
    removed_durable_entries: tuple[str, ...]
    publication_journal_removed: bool
    durable_note: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.report.name,
            "path": str(self.report.path),
            "state": self.report.state,
            "durable_dir": self.report.durable_dir,
            "size_bytes": self.report.size_bytes,
            "removed_durable_entries": list(self.removed_durable_entries),
            "publication_journal_removed": self.publication_journal_removed,
            "durable_note": self.durable_note,
        }
