"""Rebind a crash-interrupted queue claim to a replacement execution generation.

The worker child runs this before it executes a claimed row. A generation that
shows started-execution evidence is never reused: it stays frozen as that
attempt's record and a replacement generation is materialized through the
ordinary submission machinery (``build_orca_execution_snapshot`` with
``recovery_from``), seeded by ``_recovery``. Rebinds are bounded by a durable
per-row counter and claim that are consumed before any new generation exists,
so a crash loop can never mint generations indefinitely.

This module sits above the package's binding stages: it drives queue-row
mutation and the snapshot-intent ledger, so it is imported by the worker child
directly and deliberately not re-exported from the package ``__init__``
(``submission`` imports the package, and this module imports ``submission``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.queue.child.execution import find_queue_entry_by_id
from orca_auto.core.queue.child.process import entry_status_is_running
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    transition_snapshot_intent,
)
from orca_auto.core.queue.generation import (
    is_visible_generation_name,
    new_visible_generation_name,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils.persistence import timestamped_token, timestamped_token_pattern

from .config import AppConfig
from .execution import recover_crashed_state
from .execution_binding._build import build_orca_execution_snapshot
from .execution_binding._cleanup import cleanup_unowned_orca_execution_snapshot
from .execution_binding._constants import ORCA_EXECUTION_SNAPSHOT_VERSION
from .execution_binding._snapshot_identity import (
    orca_execution_snapshot_generation_dir,
    verify_orca_snapshot_executable,
)
from .execution_binding._verify import orca_execution_started_evidence
from .output_adoption import existing_completed_out
from .queue.adapter import (
    get_cancel_requested,
    list_queue,
    queue_entry_reaction_dir,
    requeue_running_entry,
    update_metadata,
)
from .queue.entries import queue_entry_is_retired_workflow_owned
from .resource_directives import prepare_submission_resource_request
from .run_lock import acquire_run_lock
from .submission import mark_orca_snapshot_owned

logger = logging.getLogger(__name__)

RECOVERY_REBIND_LIMIT = 3
RECOVERY_REBIND_COUNT_METADATA_KEY = "recovery_rebind_count"
RECOVERY_REBIND_CLAIM_METADATA_KEY = "recovery_rebind_claim"
# The durable claim replays only tokens this module minted; the validator is
# derived from the producer so the two cannot drift apart.
_RECOVERY_INTENT_TOKEN_PREFIX = "snapshot_intent"
_RECOVERY_INTENT_TOKEN_BYTES = 16
_RECOVERY_REBIND_INTENT_TOKEN_RE = timestamped_token_pattern(
    _RECOVERY_INTENT_TOKEN_PREFIX,
    token_bytes=_RECOVERY_INTENT_TOKEN_BYTES,
)


def _completed_out_or_none(bound_selected: Path) -> dict[str, Any] | None:
    try:
        return existing_completed_out(bound_selected)
    except Exception:  # noqa: BLE001
        # The output in a crashed generation is exactly the file most likely
        # to be truncated or actively racing; a probe failure must degrade to
        # the recovery path, never abort the claim.
        logger.debug(
            "completed-output probe failed for %s; continuing with recovery",
            bound_selected,
            exc_info=True,
        )
        return None


def _queue_entry_by_id(queue_root: Path, queue_id: str) -> QueueEntry | None:
    return find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue,
    )


def _validated_recovery_rebind_claim(
    metadata: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    """Validate the durable recovery budget and claim without performing I/O."""
    if (
        snapshot.get("version") != ORCA_EXECUTION_SNAPSHOT_VERSION
        or "max_retries" in snapshot
        or "max_retries" in metadata
    ):
        raise ValueError("ORCA recovery requires a current execution snapshot; resubmit the job")
    raw_count = metadata.get(RECOVERY_REBIND_COUNT_METADATA_KEY, 0)
    if (
        isinstance(raw_count, bool)
        or not isinstance(raw_count, int)
        or raw_count < 0
        or raw_count > RECOVERY_REBIND_LIMIT
    ):
        raise ValueError("ORCA crash recovery found an invalid durable rebind count")
    count = raw_count
    raw_claim = metadata.get(RECOVERY_REBIND_CLAIM_METADATA_KEY)
    pending_claim: dict[str, Any] | None = None
    if raw_claim is not None:
        if not isinstance(raw_claim, dict) or set(raw_claim) != {
            "ordinal",
            "source_generation_name",
            "intent_token",
            "target_generation_name",
        }:
            raise ValueError("ORCA crash recovery found an invalid durable rebind claim")
        ordinal = raw_claim.get("ordinal")
        raw_source_generation_name = raw_claim.get("source_generation_name")
        raw_intent_token = raw_claim.get("intent_token")
        raw_target_generation_name = raw_claim.get("target_generation_name")
        source_generation_name = (
            raw_source_generation_name if type(raw_source_generation_name) is str else ""
        )
        intent_token = raw_intent_token if type(raw_intent_token) is str else ""
        target_generation_name = (
            raw_target_generation_name if type(raw_target_generation_name) is str else ""
        )
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal <= 0
            or ordinal > RECOVERY_REBIND_LIMIT
            or ordinal != count
            or type(raw_source_generation_name) is not str
            or source_generation_name != str(snapshot.get("generation_name") or "")
            or type(raw_intent_token) is not str
            or _RECOVERY_REBIND_INTENT_TOKEN_RE.fullmatch(intent_token) is None
            or type(raw_target_generation_name) is not str
            or not is_visible_generation_name(target_generation_name)
            or target_generation_name == source_generation_name
        ):
            raise ValueError(
                "ORCA crash recovery durable rebind claim does not match the queue row"
            )
        pending_claim = dict(raw_claim)
    if raw_claim is None and count >= RECOVERY_REBIND_LIMIT:
        raise ValueError(
            "ORCA crash recovery limit reached for this submission "
            f"({count}/{RECOVERY_REBIND_LIMIT}); resubmit the job to continue"
        )
    return count, pending_claim


def _reserve_recovery_rebind_claim(
    entry: QueueEntry,
    *,
    queue_root: Path,
    snapshot: dict[str, Any],
    count: int,
    pending_claim: dict[str, Any] | None,
) -> tuple[QueueEntry, int, str, str]:
    """Reserve budget durably, or resume the already reserved generation."""
    if pending_claim is None:
        rebind_count = count + 1
        intent_token = timestamped_token(
            _RECOVERY_INTENT_TOKEN_PREFIX,
            token_bytes=_RECOVERY_INTENT_TOKEN_BYTES,
        )
        target_generation_name = new_visible_generation_name()
        pending_claim = {
            "ordinal": rebind_count,
            "source_generation_name": str(snapshot.get("generation_name") or ""),
            "intent_token": intent_token,
            "target_generation_name": target_generation_name,
        }
        if not update_metadata(
            queue_root,
            str(entry.queue_id),
            {
                RECOVERY_REBIND_COUNT_METADATA_KEY: rebind_count,
                RECOVERY_REBIND_CLAIM_METADATA_KEY: pending_claim,
            },
            expected_entry=entry,
        ):
            raise ValueError(
                "ORCA crash recovery could not reserve its durable rebind claim on the queue row"
            )
        claimed = _queue_entry_by_id(queue_root, str(entry.queue_id))
        claimed_metadata = getattr(claimed, "metadata", None)
        if (
            claimed is None
            or not isinstance(claimed_metadata, dict)
            or claimed_metadata.get(RECOVERY_REBIND_COUNT_METADATA_KEY) != rebind_count
            or claimed_metadata.get(RECOVERY_REBIND_CLAIM_METADATA_KEY) != pending_claim
        ):
            raise ValueError("ORCA crash recovery lost its durable rebind claim")
    else:
        rebind_count = count
        intent_token = str(pending_claim["intent_token"])
        target_generation_name = str(pending_claim["target_generation_name"])
        claimed = entry

    return claimed, rebind_count, intent_token, target_generation_name


def _publish_recovery_generation(
    entry: QueueEntry,
    *,
    queue_root: Path,
    reaction_dir: Path,
    claimed: QueueEntry,
    new_snapshot: dict[str, Any],
    rebind_count: int,
) -> QueueEntry:
    """Fence publication against cancellation and clean up an unowned replacement."""
    intent_token = str(new_snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "")
    try:
        transition_snapshot_intent(
            queue_root,
            intent_token,
            target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
            expected_states={SNAPSHOT_INTENT_STATE_CREATING},
        )
        if not update_metadata(
            queue_root,
            str(entry.queue_id),
            {
                "execution_snapshot": new_snapshot,
                "selected_inp": str(new_snapshot.get("selected_inp") or ""),
                RECOVERY_REBIND_CLAIM_METADATA_KEY: None,
            },
            expected_entry=claimed,
            require_running_without_cancel_requested=True,
        ):
            raise ValueError("ORCA crash recovery could not publish its replacement generation")
    except BaseException:
        cleanup_unowned_orca_execution_snapshot(reaction_dir, new_snapshot)
        raise
    marker_warning = mark_orca_snapshot_owned(queue_root, intent_token)
    updated = _queue_entry_by_id(queue_root, str(entry.queue_id))
    updated_metadata = getattr(updated, "metadata", None)
    updated_snapshot = (
        updated_metadata.get("execution_snapshot") if isinstance(updated_metadata, dict) else None
    )
    if (
        updated is None
        or not isinstance(updated_snapshot, dict)
        or str(updated_snapshot.get("generation_name") or "")
        != str(new_snapshot.get("generation_name") or "")
    ):
        raise ValueError("ORCA crash recovery lost its replacement queue row")
    logger.warning(
        "Recovered crashed ORCA job %s into replacement generation %s (rebind %d/%d)%s",
        str(entry.queue_id),
        str(new_snapshot.get("generation_name") or ""),
        rebind_count,
        RECOVERY_REBIND_LIMIT,
        f"; {marker_warning}" if marker_warning else "",
    )
    return updated


def maybe_rebind_recovery_generation(
    entry: QueueEntry,
    *,
    queue_root: Path,
    cfg_factory: Callable[[], AppConfig],
) -> QueueEntry:
    """Move a crash-interrupted claim into a fresh generation before execution.

    A generation that shows started-execution evidence is never reused: the
    crashed generation stays frozen as that attempt's record and a replacement
    generation is materialized through the ordinary submission machinery,
    seeded from the frozen runtime geometry. Rebinds are bounded by a durable
    per-row counter that is consumed before any new generation exists, so a
    crash loop can never mint generations indefinitely.

    Runs where the worker child fixes its canonical queue entry, so every
    later actor (cancel checks, shutdown requeue, terminal marking) holds the
    post-rebind publication generation.
    """

    if not entry_status_is_running(entry):
        return entry
    if queue_entry_is_retired_workflow_owned(entry, queue_root):
        raise ValueError(
            "Queued ORCA directory belongs to a retired workflow; use the previous runtime to drain or cancel it"
        )
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        return entry
    reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser().resolve()
    try:
        orca_execution_snapshot_generation_dir(reaction_dir, snapshot)
    except ValueError:
        # Let the ordinary context builder surface its canonical error.
        return entry
    if not orca_execution_started_evidence(reaction_dir, snapshot):
        return entry
    bound_selected_text = str(metadata.get("selected_inp") or "").strip()
    if bound_selected_text:
        bound_selected = Path(bound_selected_text)
        if bound_selected.is_file() and _completed_out_or_none(bound_selected) is not None:
            # ORCA finished before the crash reached the queue row. Keep the
            # generation: the ordinary claim path settles it in place (from
            # its recorded attempt verdict, else by adopting the output)
            # instead of re-running the whole calculation in a rebind.
            return entry
    if get_cancel_requested(queue_root, str(entry.queue_id), expected_entry=entry):
        # Cancellation is a generation-fenced monotonic user decision. Honor it
        # before inspecting recovery-only metadata or creating replacement state.
        requeue_running_entry(
            queue_root,
            str(entry.queue_id),
            expected_entry=entry,
        )
        refreshed = _queue_entry_by_id(queue_root, str(entry.queue_id))
        return refreshed if refreshed is not None else entry
    count, pending_claim = _validated_recovery_rebind_claim(metadata, snapshot)
    cfg = cfg_factory()
    recovery_executable = verify_orca_snapshot_executable(
        snapshot,
        expected_executable=cfg.paths.orca_executable,
    )
    claimed, rebind_count, intent_token, target_generation_name = _reserve_recovery_rebind_claim(
        entry,
        queue_root=queue_root,
        snapshot=snapshot,
        count=count,
        pending_claim=pending_claim,
    )

    source_selected = str(metadata.get("source_selected_inp") or "").strip()
    if not source_selected:
        raise ValueError("ORCA crash recovery requires the submission source input path")
    recorded_request = metadata.get("resource_request")
    with acquire_run_lock(reaction_dir):
        recover_crashed_state(reaction_dir, logger=logger)
        prepared = prepare_submission_resource_request(
            Path(source_selected),
            default_max_cores=int(cfg.resources.max_cores_per_task),
            default_max_memory_gb=int(cfg.resources.max_memory_gb_per_task),
        )
        if not isinstance(recorded_request, dict) or dict(prepared.resource_request) != dict(
            recorded_request
        ):
            raise ValueError("ORCA crash recovery resource request diverged from the queued row")
        new_snapshot = build_orca_execution_snapshot(
            reaction_dir,
            Path(source_selected),
            selected_input_xyz=str(metadata.get("selected_input_xyz") or ""),
            resource_request=prepared.resource_request,
            orca_executable=recovery_executable,
            queue_root=queue_root,
            snapshot_intent_token=intent_token,
            target_generation_name=target_generation_name,
            normalized_selected_payload=prepared.normalized_payload,
            source_selected_sha256=prepared.source_sha256,
            recovery_from=snapshot,
        )
    return _publish_recovery_generation(
        entry,
        queue_root=queue_root,
        reaction_dir=reaction_dir,
        claimed=claimed,
        new_snapshot=new_snapshot,
        rebind_count=rebind_count,
    )


__all__ = [
    "RECOVERY_REBIND_CLAIM_METADATA_KEY",
    "RECOVERY_REBIND_COUNT_METADATA_KEY",
    "RECOVERY_REBIND_LIMIT",
    "maybe_rebind_recovery_generation",
]
