"""``queue cancel`` against the queue store: find the entry, cancel it, build the payload.

The payload is embedded as ``result`` in ``orca_auto queue cancel --json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.types import QueueEntry, QueueStatus, effective_queue_status
from orca_auto.core.statuses import STATUS_FAILED
from orca_auto.core.utils import normalize_text as _normalize_text

from .queue import adapter as queue_adapter
from .queue import entries as queue_entries

_CANCEL_API_NAME = "orca_auto.orca.direct_cancel"


def _failure_payload(command_argv: list[str], stderr: str, *, reason: str = "") -> dict[str, Any]:
    if not stderr.endswith("\n"):
        stderr += "\n"
    return {
        "status": STATUS_FAILED,
        "reason": reason,
        "returncode": 1,
        "command_argv": command_argv,
        "stdout": "",
        "stderr": stderr,
        "parsed_stdout": {},
        "job_id": "",
        "queue_id": "",
        "reaction_dir": "",
        "priority": 0,
        "force": False,
    }


def _success_payload(command_argv: list[str], updated: QueueEntry) -> dict[str, Any]:
    fields = {
        "status": effective_queue_status(updated),
        "queue_id": queue_entries.queue_entry_id(updated),
        "job_id": queue_entries.queue_entry_task_id(updated),
    }
    parsed_stdout = {key: text for key, value in fields.items() if (text := _normalize_text(value))}
    return {
        "status": fields["status"],
        "reason": "",
        "returncode": 0,
        "command_argv": command_argv,
        "stdout": "\n".join(f"{key}: {value}" for key, value in parsed_stdout.items()),
        "stderr": "",
        "parsed_stdout": parsed_stdout,
        "job_id": parsed_stdout.get("job_id", ""),
        "queue_id": parsed_stdout.get("queue_id", ""),
    }


def cancel_target(*, target: str, config_path: str, allowed_root: Path) -> dict[str, Any]:
    """Cancel the ORCA queue generation ``target`` names in ``allowed_root``.

    Target precedence is :func:`.queue.adapter.find_entry_by_target`'s. When the
    cancel call returns nothing or raises, a re-read decides whether this
    generation's cancel is already durable before reporting a failure.
    """
    config_path = _normalize_text(config_path)
    target = _normalize_text(target)
    command_argv = [_CANCEL_API_NAME, f"config={config_path}", f"target={target}"]
    if not target:
        return _failure_payload(command_argv, "queue cancel requires a target")

    matched: QueueEntry | None = None
    try:
        matched = queue_adapter.find_entry_by_target(queue_adapter.list_queue(allowed_root), target)
        if matched is None:
            return _failure_payload(
                command_argv, f"queue target not found: {target}", reason="target_not_found"
            )
        queue_id = queue_entries.queue_entry_id(matched)
        updated = queue_adapter.cancel(allowed_root, queue_id, expected_entry=matched)
        if updated is None:
            current = queue_adapter.get_entry_by_id(allowed_root, queue_id)
            if (
                current is None
                or not queue_entries.is_orca_queue_entry(current)
                or not queue_entries.queue_entry_matches_target(current, target)
                or not queue_entries.same_generation(current, matched)
                or queue_entries.queue_entry_status(current) != QueueStatus.CANCELLED.value
            ):
                return _failure_payload(
                    command_argv,
                    f"queue target already terminal: {target}",
                    reason="already_terminal",
                )
            updated = current
    except Exception as exc:  # noqa: BLE001
        if matched is not None:
            try:
                current = queue_adapter.get_entry_by_id(
                    allowed_root, queue_entries.queue_entry_id(matched)
                )
                # The cancel may have committed before the error: a cancelled
                # row, or an active row flagged for its worker.
                if (
                    current is not None
                    and queue_entries.same_generation(current, matched)
                    and (
                        queue_entries.queue_entry_status(current) == QueueStatus.CANCELLED.value
                        or (
                            queue_entries.queue_entry_status(current)
                            in queue_entries.ACTIVE_STATUSES
                            and current.cancel_requested
                        )
                    )
                ):
                    return _success_payload(command_argv, current)
            except Exception:  # noqa: BLE001
                pass
        return _failure_payload(
            command_argv, f"{exc.__class__.__name__}: {exc}", reason="cancel_failed"
        )

    return _success_payload(command_argv, updated)
