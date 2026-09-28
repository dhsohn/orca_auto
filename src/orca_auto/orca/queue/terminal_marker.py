"""Durable evidence for unfinished ORCA terminal side effects.

Terminal queue state and the side effects derived from it live in different
stores.  Writers therefore persist this marker in the same queue mutation as
the terminal transition.  A fresh worker can then finish state/report/index
and notification publication without treating arbitrary historical terminal
rows as new work. ``terminal_generation_verdict`` is the one rule that decides
whether a terminal generation still owns its directory's ``job_state.json``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from orca_auto.core.queue.types import QueueEntry, QueueStatus

from ..state_reading import load_state, state_path, state_payload_job_id
from ..statuses import TERMINAL_RUN_STATUS_VALUES
from ..types import RunState
from .entries import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    TERMINAL_STATUSES,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
)

TERMINAL_REPLAY_MARKER_VERSION = 1

# How ``queue list`` reports a row whose marker is still present: the blocker
# scope, the process that finishes the publication, and the reason and next
# action for a valid and for an invalid marker.
TERMINAL_PUBLICATION_SCOPE = "orca_terminal_publication"
TERMINAL_PUBLICATION_OWNER = "orca_queue_worker"
TERMINAL_PUBLICATION_PENDING_REASON = "terminal publication pending"
TERMINAL_PUBLICATION_PENDING_ACTION = (
    "The queue worker retries result publication automatically. Inspect the worker log "
    "if it persists; this directory remains fenced until publication finishes."
)
TERMINAL_PUBLICATION_INVALID_REASON = "terminal publication marker invalid"
TERMINAL_PUBLICATION_INVALID_ACTION = (
    "Inspect the worker log and repair the invalid replay marker before resubmitting."
)


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
    return status if status in TERMINAL_RUN_STATUS_VALUES else None


def state_generation_fingerprint(
    state: RunState | None,
    *,
    present: bool,
) -> StateGenerationFingerprint:
    """The fingerprint of a loaded state; a present file that did not load is unreadable."""
    if state is None:
        return StateGenerationFingerprint(present=present, readable=not present)
    return StateGenerationFingerprint(
        present=True,
        readable=True,
        job_id=state_payload_job_id(state),
        run_id=str(state.get("run_id") or "").strip(),
        terminal_status=terminal_status_from_run_state(state) or "",
    )


def load_state_generation_fingerprint(
    reaction_dir: Path,
) -> StateGenerationFingerprint:
    state_file = state_path(reaction_dir)
    present = state_file.exists()
    try:
        state = load_state(reaction_dir)
    except Exception:  # noqa: BLE001 - unreadability is part of the fingerprint
        return StateGenerationFingerprint(present=True, readable=False)
    return state_generation_fingerprint(state, present=present or state_file.exists())


class TerminalGenerationVerdict(str, Enum):
    """How a directory's current state relates to one terminal generation.

    ``terminal_generation_verdict`` decides it for both readers. The replay
    pre-check (``settlement.is_superseded``) drops the generation only on a
    verdict that proves a newer one; the writer under ``run.lock``
    (``terminal_state``) writes for OWNED, ABSENT and both PREVIOUS_TERMINAL
    verdicts and raises for every other. The ``_UNVERIFIED`` and
    ``UNOBSERVED_`` verdicts are where the two readers answer differently.

    The fourteen members keep both readers' answers as
    ``tests/contracts/pins/replay_supersession.json`` pins them, not a minimal
    classification. ``is_superseded`` adds rules of its own before it asks for
    a verdict: an item with an empty reaction directory or task id is
    superseded.
    """

    UNREADABLE = "unreadable"  # the state file exists but does not load
    ABSENT = "absent"  # no state, and the mark observed none (or nothing)
    DISAPPEARED = "disappeared"  # the state the mark observed is gone
    DISAPPEARED_UNVERIFIED = "disappeared_unverified"  # gone; the mark could not read it
    OWNED = "owned"  # this generation's state
    RESTARTED = "restarted"  # this task runs again after the mark observed another job
    NEWER_RUN = "newer_run"  # this task's state carries another run id
    REPLACED = "replaced"  # another job's state, unlike the one the mark observed
    REPLACED_UNVERIFIED = "replaced_unverified"  # another job's state; the mark could not read
    OTHER_ACTIVE = "other_active"  # another job's active state, as the mark observed it
    PREVIOUS_TERMINAL = "previous_terminal"  # a finished state as observed, or without a job id
    UNIDENTIFIED_ACTIVE = "unidentified_active"  # an active state without a job id
    UNOBSERVED_PREVIOUS_TERMINAL = "unobserved_previous_terminal"  # no mark; another job finished
    UNOBSERVED_OTHER_ACTIVE = "unobserved_other_active"  # no mark; another job's active state


def terminal_generation_verdict(
    current: StateGenerationFingerprint,
    *,
    task_id: str,
    observed: StateGenerationFingerprint | None,
    expected_run_id: str,
) -> TerminalGenerationVerdict:
    """Relate the directory's ``current`` state to the terminal generation of ``task_id``.

    ``observed`` is the state the terminal mark recorded (``None`` when none
    was recorded) and ``expected_run_id`` the run the caller expects this
    task's state to carry (empty skips the run check). The rule fails closed:
    a state that may belong to a newer generation is never OWNED.
    """
    verdict = TerminalGenerationVerdict
    if not current.readable:
        return verdict.UNREADABLE
    if not current.present:
        if observed is None or observed == current:
            return verdict.ABSENT
        return verdict.DISAPPEARED if observed.readable else verdict.DISAPPEARED_UNVERIFIED
    if not task_id:
        return verdict.OWNED
    if current.job_id == task_id:
        if (
            observed is not None
            and observed.readable
            and observed.job_id
            and observed.job_id != task_id
            and not current.terminal_status
        ):
            return verdict.RESTARTED
        if expected_run_id and current.run_id and current.run_id != expected_run_id:
            return verdict.NEWER_RUN
        return verdict.OWNED
    if observed is None:
        if current.job_id:
            return (
                verdict.UNOBSERVED_PREVIOUS_TERMINAL
                if current.terminal_status
                else verdict.UNOBSERVED_OTHER_ACTIVE
            )
    elif not observed.readable:
        return verdict.REPLACED_UNVERIFIED
    elif current != observed:
        return verdict.REPLACED
    elif current.job_id and not current.terminal_status:
        return verdict.OTHER_ACTIVE
    if current.terminal_status:
        # A forced submission can reuse a reaction directory before the new
        # child writes state. A complete terminal result is durable evidence
        # that this is the previous generation.
        return verdict.PREVIOUS_TERMINAL
    # A nonterminal mismatch can be the current active generation; an old
    # finalizer must not publish a terminal result over it.
    return verdict.UNIDENTIFIED_ACTIVE


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


def terminal_replay_metadata_update_fn(
    *,
    status: QueueStatus,
    error: str,
    metadata_update: Mapping[str, Any] | None = None,
    allow_terminal_candidate: bool = False,
) -> Callable[[QueueEntry], Mapping[str, Any] | None]:
    """Return the store ``metadata_update_fn`` that attaches the replay marker.

    Every ORCA terminal writer passes this to the queue store so the marker is
    persisted in the same queue mutation as the terminal transition.
    """
    supplied_metadata = dict(metadata_update or {})

    def update(current: QueueEntry) -> Mapping[str, Any] | None:
        if supplied_metadata.get(TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY) is True:
            raise ValueError(
                "terminal side-effect replay and an administrative fence are mutually exclusive"
            )
        if not allow_terminal_candidate and current.status not in {
            QueueStatus.PENDING,
            QueueStatus.RUNNING,
        }:
            # Core terminal marks permit idempotent same-status calls.  Once a
            # completed marker has been cleared, such a call must not resurrect
            # replay work for closed history.  Explicit metadata still follows
            # the caller's request through the static update path.
            return None
        candidate_metadata = dict(current.metadata)
        candidate_metadata.update(supplied_metadata)
        candidate = replace(current, metadata=candidate_metadata)
        return {
            TERMINAL_REPLAY_METADATA_KEY: terminal_replay_marker_for_entry(
                candidate,
                status=status.value,
                error=error,
            )
        }

    return update


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
