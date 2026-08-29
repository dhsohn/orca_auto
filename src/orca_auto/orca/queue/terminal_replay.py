"""Durable evidence for unfinished ORCA terminal side effects.

Terminal queue state and the side effects derived from it live in different
stores.  Writers therefore persist this marker in the same queue mutation as
the terminal transition.  A fresh worker can then finish state/report/index
and notification publication without treating arbitrary historical terminal
rows as new work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..state_reading import load_state, state_path, state_payload_job_id
from ..types import RunState
from .entries import (
    TERMINAL_STATUSES,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
)

TERMINAL_REPLAY_METADATA_KEY = "orca_terminal_replay"
TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY = "orca_terminal_replay_fence_only"
TERMINAL_REPLAY_MARKER_VERSION = 1


class TerminalReplayMarkerKind(str, Enum):
    """Classification shared by replay admission and queue retention."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID_OR_UNSUPPORTED = "invalid_or_unsupported"


@dataclass(frozen=True)
class StateGenerationFingerprint:
    present: bool
    readable: bool
    job_id: str = ""
    run_id: str = ""
    terminal_status: str = ""


def terminal_status_from_run_state(state: RunState | None) -> str | None:
    if state is None:
        return None
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return None
    status = str(final_result.get("status") or "").strip().lower()
    return status if status in TERMINAL_STATUSES else None


def load_state_generation_fingerprint(
    reaction_dir: Path,
) -> StateGenerationFingerprint:
    state_file = state_path(reaction_dir)
    present = state_file.exists()
    try:
        state = load_state(reaction_dir)
    except Exception:  # noqa: BLE001 - unreadability is part of the fingerprint
        return StateGenerationFingerprint(present=True, readable=False)
    present = present or state_file.exists()
    if state is None:
        return StateGenerationFingerprint(present=present, readable=not present)
    return StateGenerationFingerprint(
        present=True,
        readable=True,
        job_id=state_payload_job_id(state),
        run_id=str(state.get("run_id") or "").strip(),
        terminal_status=terminal_status_from_run_state(state) or "",
    )


def state_fingerprint_payload(
    fingerprint: StateGenerationFingerprint,
) -> dict[str, Any]:
    return {
        "present": fingerprint.present,
        "readable": fingerprint.readable,
        "job_id": fingerprint.job_id,
        "run_id": fingerprint.run_id,
        "terminal_status": fingerprint.terminal_status,
    }


def state_fingerprint_from_payload(payload: Any) -> StateGenerationFingerprint | None:
    if not isinstance(payload, dict):
        return None
    present = payload.get("present")
    readable = payload.get("readable")
    if not isinstance(present, bool) or not isinstance(readable, bool):
        return None
    terminal_status = str(payload.get("terminal_status") or "").strip().lower()
    if terminal_status and terminal_status not in TERMINAL_STATUSES:
        return None
    return StateGenerationFingerprint(
        present=present,
        readable=readable,
        job_id=str(payload.get("job_id") or "").strip(),
        run_id=str(payload.get("run_id") or "").strip(),
        terminal_status=terminal_status,
    )


def terminal_replay_marker(
    *,
    reaction_dir: str,
    task_id: str | None,
    selected_inp: str,
    status: str,
    error: str,
) -> dict[str, Any]:
    reaction_text = str(reaction_dir or "").strip()
    fingerprint = (
        load_state_generation_fingerprint(Path(reaction_text).expanduser().resolve())
        if reaction_text
        else StateGenerationFingerprint(present=False, readable=True)
    )
    return {
        "version": TERMINAL_REPLAY_MARKER_VERSION,
        "task_id": str(task_id or "").strip(),
        "selected_inp": str(selected_inp or "").strip(),
        "status": str(status or "").strip().lower(),
        "error": str(error or "").strip(),
        "observed_state": state_fingerprint_payload(fingerprint),
    }


def terminal_replay_marker_for_entry(
    entry: Any,
    *,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    metadata = queue_entry_metadata(entry)
    selected_inp = str(
        metadata.get("selected_inp") or metadata.get("selected_input_path") or ""
    ).strip()
    return terminal_replay_marker(
        reaction_dir=queue_entry_reaction_dir(entry),
        task_id=queue_entry_task_id(entry),
        selected_inp=selected_inp,
        status=status,
        error=error,
    )


def terminal_replay_marker_from_entry(entry: Any) -> dict[str, Any] | None:
    marker = queue_entry_metadata(entry).get(TERMINAL_REPLAY_METADATA_KEY)
    if not isinstance(marker, dict):
        return None
    version = marker.get("version")
    if type(version) is not int or version != TERMINAL_REPLAY_MARKER_VERSION:
        return None
    if state_fingerprint_from_payload(marker.get("observed_state")) is None:
        return None
    marker_status = str(marker.get("status") or "").strip().lower()
    if marker_status not in TERMINAL_STATUSES:
        return None
    marker_task_id = str(marker.get("task_id") or "").strip()
    entry_task_id = str(queue_entry_task_id(entry) or "").strip()
    if not marker_task_id or not entry_task_id or marker_task_id != entry_task_id:
        return None
    return marker


def terminal_replay_marker_kind(entry: Any) -> TerminalReplayMarkerKind:
    metadata = queue_entry_metadata(entry)
    fence_only = metadata.get(TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY)
    if fence_only is not None and not isinstance(fence_only, bool):
        return TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
    marker_present = (
        TERMINAL_REPLAY_METADATA_KEY in metadata
        and metadata.get(TERMINAL_REPLAY_METADATA_KEY) is not None
    )
    if fence_only is True and marker_present:
        # Administrative fence-only rows and side-effect-bearing markers are
        # mutually exclusive.  Preserve a contradictory generation for manual
        # repair instead of guessing which lifecycle contract owns it.
        return TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
    if not marker_present:
        return TerminalReplayMarkerKind.ABSENT
    if terminal_replay_marker_from_entry(entry) is not None:
        return TerminalReplayMarkerKind.VALID
    return TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED


def terminal_replay_is_fence_only(entry: Any) -> bool:
    return queue_entry_metadata(entry).get(TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY) is True
