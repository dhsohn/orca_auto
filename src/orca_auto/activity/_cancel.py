"""``cancel_activity``: resolve the target once over the queue catalog and cancel that row."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.activity.model import ActivityRecord, path_aliases, sort_key
from orca_auto.core.queue.types import QueueEntry, QueueStatus, effective_queue_status
from orca_auto.core.statuses import ACTIVE_STATUSES, STATUS_FAILED, is_queue_active_status
from orca_auto.core.utils import normalize_text
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import entries as queue_entries

from . import _orca

CatalogRow = tuple[QueueEntry, ActivityRecord]


def _resolved_target(target: str) -> str:
    try:
        return str(Path(target).expanduser().resolve())
    except (OSError, RuntimeError):
        return ""


def _row_directory(record: ActivityRecord) -> tuple[str, ...]:
    return path_aliases(normalize_text(record.metadata.get("reaction_dir")))[:1]


def target_rows(rows: list[CatalogRow], target: str) -> tuple[list[CatalogRow], str]:
    """The rows ``target`` names, and a directory it names that none of them has (or "").

    A target equal to a row's queue ID or run ID (the one the row records, or a
    running row's from its own state) names that row before any alias.
    Otherwise it matches a row's job ID or directory: absolute, relative to
    ``runs_root`` or to the working directory, or its name. Matches in several
    directories are ambiguous. Within one directory the active generation wins
    and two active ones are ambiguous; with none active the newest finished row
    answers, so a retry observes an already-cancelled outcome.

    A target that is an existing directory relative to the working directory
    names that directory. When no matched row has it, the matches come back
    with it and the target is ambiguous: ``foo`` inside a directory holding its
    own ``foo`` never falls back to another directory's ``foo``.
    """
    named = [row for row in rows if target in row[1].ids]
    if named:
        return named, ""
    resolved = _resolved_target(target)
    wanted = {target, resolved} - {""}
    matches = [row for row in rows if wanted.intersection(row[1].aliases)]
    directories = {_row_directory(record) for _entry, record in matches}
    if matches and resolved and (resolved,) not in directories and Path(resolved).is_dir():
        return matches, resolved
    if len(matches) > 1 and (len(directories) > 1 or () in directories):
        return matches, ""
    active = [row for row in matches if is_queue_active_status(row[1].status)]
    if active:
        return active, ""
    return ([max(matches, key=lambda row: sort_key(row[1]))] if matches else []), ""


def _payload(status: str, reason: str = "", record: ActivityRecord | None = None) -> dict[str, Any]:
    """The ``queue cancel`` document; its row fields stay empty when no single row was named."""
    row = record.to_dict() if record is not None else {"metadata": {}}
    metadata = row["metadata"]
    return {
        **{key: row.get(key, "") for key in ("activity_id", "kind", "engine", "source", "label")},
        "status": status,
        "cancel_target": row.get("cancel_target", ""),
        "result": {
            "status": status,
            "reason": reason,
            "queue_id": metadata.get("queue_id", ""),
            "job_id": metadata.get("task_id", ""),
            "reaction_dir": metadata.get("reaction_dir", ""),
        },
    }


def _committed_cancel(runs_root: Path, matched: QueueEntry) -> QueueEntry | None:
    """This generation's row when its cancel is durable: cancelled, or flagged while active."""
    current = queue_adapter.get_entry_by_id(runs_root, queue_entries.queue_entry_id(matched))
    if current is None or not queue_entries.same_generation(current, matched):
        return None
    status = queue_entries.queue_entry_status(current)
    if status == QueueStatus.CANCELLED.value or (
        status in ACTIVE_STATUSES and current.cancel_requested
    ):
        return current
    return None


def cancel_activity(*, target: str, runs_root: Path) -> tuple[dict[str, Any], str]:
    """The ``queue cancel`` payload and, when nothing was cancelled, the error message."""
    target = normalize_text(target)
    if not target:
        return _payload(STATUS_FAILED, "target_not_found"), "Cancel target is empty."
    rows, directory = target_rows(_orca.catalog(runs_root), target)
    if not rows:
        return _payload(STATUS_FAILED, "target_not_found"), f"Activity target not found: {target}"
    if len(rows) > 1 or directory:
        names = [record.activity_id for _entry, record in rows]
        if directory:
            names.append(directory)
        matches = ", ".join(sorted(names))
        return (
            _payload(STATUS_FAILED, "ambiguous"),
            f"Ambiguous activity target: {target}. Matches: {matches}",
        )
    [(matched, record)] = rows
    try:
        updated = queue_adapter.cancel(
            runs_root, queue_entries.queue_entry_id(matched), expected_entry=matched
        )
        if updated is None:
            # Refused: finished, removed or replaced. Only this generation's
            # cancelled row repeats a success.
            updated = _committed_cancel(runs_root, matched)
            if (
                updated is None
                or queue_entries.queue_entry_status(updated) != QueueStatus.CANCELLED.value
            ):
                return (
                    _payload(STATUS_FAILED, "already_terminal", record),
                    f"queue target already terminal: {record.cancel_target}",
                )
    except Exception as exc:  # noqa: BLE001
        # The cancel may have committed before the error.
        try:
            updated = _committed_cancel(runs_root, matched)
        except Exception:  # noqa: BLE001
            updated = None
        if updated is None:
            return (
                _payload(STATUS_FAILED, "cancel_failed", record),
                f"{exc.__class__.__name__}: {exc}",
            )
    return _payload(effective_queue_status(updated), "", record), ""
