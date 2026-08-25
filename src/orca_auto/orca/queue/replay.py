from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    list_slots,
    reconcile_stale_slots,
    recover_orphaned_engine_slots,
    recover_slot_engine_process,
    update_slot_metadata,
)
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue.lifecycle import (
    EngineQueueProcessLifecycleHooks,
    EngineQueueProcessReconcileHooks,
    EngineQueueProcessShutdownHooks,
    EngineQueueTerminalSideEffectHooks,
    attach_started_process_metadata,
    mark_terminal_process_queue_entry_with_result,
    reconcile_orphaned_process_entries,
    shutdown_running_process_job,
)
from orca_auto.core.queue.lifecycle import job_queue_root as _lifecycle_job_queue_root
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import EngineRunningJob as RunningJob
from orca_auto.core.queue.worker import (
    live_queue_slot_keys_for_slots,
    terminate_process_group,
)
from orca_auto.core.statuses import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from orca_auto.orca.worker_execution import BackgroundRunJobProcess

from ..attempt.reporting import build_final_result, last_out_path_from_state
from ..config import AppConfig
from ..engine import ENGINE_RUNTIME
from ..execution_binding import orca_execution_provenance
from ..run_lock import acquire_run_lock
from ..state import (
    finalize_state,
    load_state,
    new_state,
    state_path,
    write_report_files,
)
from ..statuses import AnalyzerStatus
from ..types import RunState
from . import worker_tracking
from .adapter import (
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    queue_entry_app_name,
    queue_entry_id,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    reconcile_orphaned_running_entries,
    requeue_running_entry,
    update_terminal,
)
from .adapter import update_metadata as update_queue_metadata
from .terminal_replay import (
    TERMINAL_REPLAY_METADATA_KEY,
    StateGenerationFingerprint,
    TerminalReplayMarkerKind,
    load_state_generation_fingerprint,
    state_fingerprint_from_payload,
    terminal_replay_is_fence_only,
    terminal_replay_marker_from_entry,
    terminal_replay_marker_kind,
    terminal_status_from_run_state,
)

logger = logging.getLogger(__name__)

ACTIVE_QUEUE_STATUSES = frozenset({"pending", "running"})
TERMINAL_QUEUE_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED})
TERMINAL_FINALIZE_RETRY_ATTR = "_orca_terminal_finalize_retry_pending"


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return ENGINE_RUNTIME.queue_roots(cfg)


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, Any]]:
    return ENGINE_RUNTIME.queue_entries_with_roots(
        cfg,
        list_queue_fn=lambda root: list_queue(Path(root)),
    )


def terminate_process(process: Any) -> bool:
    return terminate_process_group(process)


@dataclass(frozen=True)
class ReactionGenerationRow:
    owner: tuple[str, str]
    task_id: str
    status: str
    transitioned_from_active: bool = False
    pending_replay: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_QUEUE_STATUSES


@dataclass(frozen=True)
class ArtifactGeneration:
    readable: bool
    state_job_id: str = ""


@dataclass(frozen=True)
class TerminalReplayWorkItem:
    queue_root: Path
    queue_id: str
    reaction_dir: str
    reaction_key: str
    task_id: str
    observed_status: str
    selected_inp: str
    error: str
    execution_provenance: dict[str, Any] | None = None
    recorded_run_id: str = ""
    resolved_status: str = ""
    run_id: str | None = None
    state_prepared: bool = False
    observed_state: StateGenerationFingerprint | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.queue_root), self.queue_id)


@dataclass
class OrcaWorkerReplayState:
    """The worker's terminal-replay bookkeeping, in one typed place.

    ``_after_orca_worker_init`` attaches one instance per worker;
    ``replay_state`` is the only accessor. ``reconcile_statuses`` stays
    ``None`` until the first reconcile pass seeds the startup cursor, so a
    terminal row first seen after startup is treated as closed history rather
    than a fresh active-to-terminal transition.
    """

    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem] = field(default_factory=dict)
    reconcile_statuses: dict[tuple[str, str], str] | None = None
    blocked_marker_keys: set[tuple[str, str]] = field(default_factory=set)
    generation_owners: dict[str, tuple[str, str]] = field(default_factory=dict)
    generation_owner_active: dict[str, bool] = field(default_factory=dict)


def get_replay_state(worker: Any) -> OrcaWorkerReplayState:
    state = getattr(worker, "engine_state", None)
    if not isinstance(state, OrcaWorkerReplayState):
        state = OrcaWorkerReplayState()
        worker.engine_state = state
    return state


def job_pending_replay_item(job: Any) -> TerminalReplayWorkItem | None:
    item = getattr(job, "_orca_terminal_replay_item", None)
    return item if isinstance(item, TerminalReplayWorkItem) else None


def terminal_replay_blocks_new_generation(worker: Any) -> bool:
    if get_replay_state(worker).pending_replays:
        return True
    for _queue_id, job in worker._running_jobs():
        if job_pending_replay_item(job) is not None:
            return True
        if bool(getattr(job, TERMINAL_FINALIZE_RETRY_ATTR, False)):
            return True
    return False


def queue_entry_by_id(queue_root: Any, target_queue_id: str) -> QueueEntry | None:
    for entry in list_queue(Path(queue_root)):
        if queue_entry_id(entry) == target_queue_id and entry_matches_engine_identity(
            entry, "orca"
        ):
            return entry
    return None


def reaction_generation_key(entry: Any) -> str | None:
    reaction_dir = queue_entry_reaction_dir(entry)
    if not reaction_dir:
        return None
    return str(Path(reaction_dir).expanduser().resolve())


def _expected_queue_entry(queue_root: Any, queue_id: str) -> QueueEntry | None:
    return queue_entry_by_id(queue_root, queue_id)


def _mark_failed_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and mark_failed(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _mark_cancelled_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and mark_cancelled(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _mark_completed_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and mark_completed(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _cancel_requested_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and get_cancel_requested(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _requeue_running_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and requeue_running_entry(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def orca_worker_lifecycle_hooks() -> EngineQueueProcessLifecycleHooks:
    return EngineQueueProcessLifecycleHooks(
        queue_entry_id_fn=queue_entry_id,
        queue_entry_app_name_fn=queue_entry_app_name,
        queue_entry_task_id_fn=queue_entry_task_id,
        update_slot_metadata_fn=update_slot_metadata,
        terminate_process_fn=terminate_process,
        mark_failed_fn=_mark_failed_expected,
        upsert_running_job_record_fn=worker_tracking.upsert_running_job_record,
        get_run_id_from_state_fn=worker_tracking.get_run_id_from_state,
        get_cancel_requested_fn=_cancel_requested_expected,
        mark_cancelled_fn=_mark_cancelled_expected,
        mark_completed_fn=_mark_completed_expected,
        upsert_terminal_job_record_fn=worker_tracking.upsert_terminal_job_record,
        notify_terminal_job_from_state_fn=worker_tracking.notify_terminal_job_from_state,
        find_queue_entry_fn=queue_entry_by_id,
        on_completed_fn=None,
        terminal_side_effect_hooks=EngineQueueTerminalSideEffectHooks(
            upsert_terminal_job_record_fn=worker_tracking.upsert_terminal_job_record,
            notify_terminal_job_from_state_fn=worker_tracking.notify_terminal_job_from_state,
        ),
    )


def shutdown_running_job(
    worker: Any,
    queue_id: str,
    job: Any,
    *,
    terminate_process_fn: Callable[[Any], Any],
) -> None:
    shutdown_running_process_job(
        worker,
        queue_id,
        job,
        hooks=EngineQueueProcessShutdownHooks(
            terminate_process_fn=terminate_process_fn,
            requeue_running_entry_fn=_requeue_running_expected,
        ),
    )


def job_queue_root(worker: Any, job: Any) -> Path:
    return _lifecycle_job_queue_root(worker, job)


def handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
) -> None:
    queue_id = queue_entry_id(entry)
    logger.error("Failed to start job %s: %s", queue_id, exc)
    worker._mark_entry_failed_and_release(
        queue_root,
        entry,
        admission_token,
        error=str(exc),
        mark_failed_fn=mark_failed,
    )


def on_worker_process_started(
    worker: Any,
    queue_root: Path,
    entry: Any,
    process: BackgroundRunJobProcess,
    admission_token: str,
) -> bool:
    return attach_started_process_metadata(
        worker,
        queue_root,
        entry,
        process=process,
        admission_token=admission_token,
        hooks=orca_worker_lifecycle_hooks(),
    )


def _finalize_finished_job(worker: Any, queue_id: str, job: RunningJob, *, rc: int) -> None:
    # A child can exit while its engine process is still recorded as active.  Do
    # not publish a terminal queue state (or make the capacity reusable) until
    # that identity has been recovered.  Raising here deliberately leaves the
    # completed job in ``_running`` so the worker retries the whole finalization.
    job.__dict__[TERMINAL_FINALIZE_RETRY_ATTR] = True
    recover_slot_engine_process(worker.admission_root, job.admission_token)
    pending_item = job_pending_replay_item(job)
    if pending_item is not None:
        release_slot_after_finalize = False
        try:
            strictly_finish_terminal_replay(worker, job, pending_item)
            release_slot_after_finalize = True
        finally:
            if release_slot_after_finalize:
                worker._release_admission_slot(job.admission_token)
                job.__dict__.pop("_orca_terminal_replay_item", None)
                job.__dict__.pop(TERMINAL_FINALIZE_RETRY_ATTR, None)
        return

    mark_result = mark_terminal_process_queue_entry_with_result(
        worker,
        queue_id,
        job,
        rc=rc,
        hooks=orca_worker_lifecycle_hooks(),
    )
    release_slot_after_finalize = False
    try:
        # A no-op is benign only when another actor already moved or removed the
        # queue row. The mark result carries a pre-mark snapshot, so re-read before
        # deciding: an actually RUNNING row has no durable terminal owner and must
        # retain the job and slot for the supervised completion retry.
        current_after_mark = queue_entry_by_id(mark_result.queue_root, queue_id)
        if normalized_entry_status(current_after_mark) == STATUS_RUNNING:
            raise RuntimeError(
                "terminal queue mark did not update the running entry; "
                f"retaining retry ownership for {queue_id}"
            )
        marker = (
            terminal_replay_marker_from_entry(current_after_mark)
            if current_after_mark is not None
            else None
        )
        if marker is not None:
            assert current_after_mark is not None
            reaction_dir = queue_entry_reaction_dir(current_after_mark)
            reaction_key = reaction_generation_key(current_after_mark)
            if not reaction_dir or not reaction_key:
                raise RuntimeError(
                    f"terminal replay marker has no durable reaction identity: {queue_id}"
                )
            item = new_terminal_replay_work_item(
                mark_result.queue_root,
                current_after_mark,
                reaction_dir=reaction_dir,
                reaction_key=reaction_key,
            )
            strictly_finish_terminal_replay(worker, job, item)
        elif mark_result.marked:
            logger.info(
                "Terminal queue generation was already closed before finalizer replay: %s",
                queue_id,
            )
        release_slot_after_finalize = True
    finally:
        if release_slot_after_finalize:
            worker._release_admission_slot(job.admission_token)
            job.__dict__.pop("_orca_terminal_replay_item", None)
            job.__dict__.pop(TERMINAL_FINALIZE_RETRY_ATTR, None)


def finalize_completed_job(worker: Any, queue_id: str, job: Any, rc: int) -> None:
    _finalize_finished_job(worker, queue_id, job, rc=rc)


def finalize_child_exit(worker: Any, job: RunningJob, *, rc: int) -> None:
    _finalize_finished_job(worker, job.queue_id, job, rc=rc)


def normalized_entry_status(entry: Any) -> str:
    raw_status = getattr(entry, "status", None)
    return str(getattr(raw_status, "value", raw_status) or "").strip().lower()


def _clear_terminal_replay_marker(item: TerminalReplayWorkItem) -> bool:
    return update_queue_metadata(
        item.queue_root,
        item.queue_id,
        {TERMINAL_REPLAY_METADATA_KEY: None},
    )


def _load_artifact_generation(reaction_key: str) -> ArtifactGeneration:
    reaction_dir = Path(reaction_key)
    state_file = state_path(reaction_dir)
    state_existed = state_file.exists()
    try:
        state = load_state(reaction_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read ORCA state generation for %s: %s", reaction_dir, exc)
        return ArtifactGeneration(readable=False)
    if (state_existed or state_file.exists()) and state is None:
        logger.warning("Failing closed on unreadable ORCA state generation: %s", state_file)
        return ArtifactGeneration(readable=False)

    return ArtifactGeneration(
        readable=True,
        state_job_id=worker_tracking.payload_job_id(state),
    )


def _select_generation_owner(
    rows: list[ReactionGenerationRow],
    *,
    previous_owner: tuple[str, str] | None,
    previous_owner_was_active: bool,
    artifacts: ArtifactGeneration,
) -> tuple[str, str] | None:
    def choose(candidates: list[ReactionGenerationRow]) -> tuple[str, str] | None:
        if len(candidates) == 1:
            return candidates[0].owner
        if previous_owner is not None and any(row.owner == previous_owner for row in candidates):
            return previous_owner
        task_ids = {row.task_id for row in candidates}
        if len(task_ids) == 1 and "" not in task_ids:
            return min(row.owner for row in candidates)
        return None

    # Any live generation fences every terminal replay for this reaction dir.
    # Multiple live entries are ambiguous: timestamps and queue ordering cannot
    # prove which child owns the shared artifacts.
    active_rows = [row for row in rows if row.active]
    if active_rows:
        return choose(active_rows) if len(active_rows) == 1 else None

    # A transition observed in this poll (or for the prior selected owner) is
    # stronger evidence than a stale state left by the preceding generation.
    transition_rows = [
        row
        for row in rows
        if row.transitioned_from_active
        or (row.owner == previous_owner and previous_owner_was_active)
    ]
    if transition_rows:
        if artifacts.state_job_id:
            matching_transition = [
                row for row in transition_rows if row.task_id == artifacts.state_job_id
            ]
            selected = choose(matching_transition)
            if selected is not None:
                return selected
        return choose(transition_rows)

    if not artifacts.readable:
        return None

    # State is authoritative.  A mismatching explicit identity means the visible
    # terminal entries do not own the current reaction-dir generation.
    if artifacts.state_job_id:
        return choose([row for row in rows if row.task_id == artifacts.state_job_id])

    pending_rows = [row for row in rows if row.pending_replay]
    if pending_rows:
        selected = choose(pending_rows)
        if selected is not None:
            return selected

    if len(rows) == 1:
        return rows[0].owner

    if len({row.task_id for row in rows}) == 1 and rows[0].task_id:
        return choose(rows)
    return None


def new_terminal_replay_work_item(
    queue_root: Any,
    entry: Any,
    *,
    reaction_dir: str,
    reaction_key: str,
) -> TerminalReplayWorkItem:
    metadata = queue_entry_metadata(entry)
    snapshot = metadata.get("execution_snapshot")
    try:
        execution_provenance = (
            orca_execution_provenance(snapshot) if isinstance(snapshot, Mapping) else None
        )
    except (TypeError, ValueError):
        execution_provenance = None
    marker = terminal_replay_marker_from_entry(entry)
    selected_inp = str(
        (marker or {}).get("selected_inp")
        or metadata.get("selected_inp")
        or metadata.get("selected_input_path")
        or ""
    ).strip()
    observed_state = state_fingerprint_from_payload((marker or {}).get("observed_state"))
    if observed_state is None:
        observed_state = load_state_generation_fingerprint(
            Path(reaction_dir).expanduser().resolve()
        )
    return TerminalReplayWorkItem(
        queue_root=Path(queue_root).expanduser().resolve(),
        queue_id=queue_entry_id(entry),
        reaction_dir=reaction_dir,
        reaction_key=reaction_key,
        task_id=str((marker or {}).get("task_id") or queue_entry_task_id(entry) or "").strip(),
        observed_status=normalized_entry_status(entry),
        selected_inp=selected_inp,
        error=str((marker or {}).get("error") or getattr(entry, "error", "") or "queue_failed"),
        execution_provenance=execution_provenance,
        recorded_run_id=str(metadata.get("run_id") or "").strip(),
        observed_state=observed_state,
    )


def _prepare_terminal_replay_work_item(
    item: TerminalReplayWorkItem,
) -> TerminalReplayWorkItem:
    run_id: str | None = None
    terminal_status: str | None = item.observed_status
    reaction_text = str(item.reaction_dir or "").strip()
    if not reaction_text:
        raise RuntimeError("terminal replay has no reaction directory")
    reaction_dir = Path(reaction_text).expanduser().resolve()
    if item.observed_status == STATUS_FAILED:
        run_id, terminal_status = record_failed_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            reason=item.error,
            observed_state=item.observed_state,
            execution_provenance=item.execution_provenance,
        )
    elif item.observed_status == STATUS_CANCELLED:
        run_id, terminal_status = record_cancelled_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            observed_state=item.observed_state,
            execution_provenance=item.execution_provenance,
        )
    resolved_status = (
        terminal_status
        if terminal_status in (STATUS_COMPLETED, STATUS_CANCELLED)
        else STATUS_FAILED
    )
    return replace(
        item,
        resolved_status=resolved_status,
        run_id=run_id,
        state_prepared=True,
    )


def _run_terminal_replay_side_effects(
    worker: Any,
    item: TerminalReplayWorkItem,
) -> None:
    if not str(item.reaction_dir or "").strip():
        raise RuntimeError("terminal replay has no reaction directory")
    record_upserted = worker_tracking.upsert_terminal_job_record(
        worker.cfg,
        item.reaction_dir,
        fallback_job_id=item.task_id,
        expected_job_id=item.task_id,
    )
    if not record_upserted:
        raise RuntimeError("terminal job record artifacts are not ready")
    worker_tracking.notify_terminal_job_from_state(
        worker.cfg,
        item.reaction_dir,
        expected_job_id=item.task_id,
    )
    if worker.cfg.messenger.enabled:
        replayed_state = load_state(Path(item.reaction_dir).expanduser().resolve())
        if (
            not replayed_state
            or not worker_tracking.payload_matches_expected_job_id(
                replayed_state,
                item.task_id,
            )
            or not worker_tracking.finished_notification_already_sent(replayed_state)
        ):
            raise RuntimeError("terminal notification was not durably recorded")


def _clear_terminal_replay_marker_or_confirm_absent(item: TerminalReplayWorkItem) -> None:
    if _clear_terminal_replay_marker(item):
        return
    current = queue_entry_by_id(item.queue_root, item.queue_id)
    if current is None or terminal_replay_marker_from_entry(current) is None:
        return
    raise RuntimeError(
        f"terminal replay marker remained after successful side effects: queue_id={item.queue_id}"
    )


def strictly_finish_terminal_replay(
    worker: Any,
    job: RunningJob,
    item: TerminalReplayWorkItem,
) -> None:
    """Finish one durable terminal generation before making its slot reusable."""

    job.__dict__["_orca_terminal_replay_item"] = item
    if _pending_replay_state_is_superseded(item):
        _clear_terminal_replay_marker_or_confirm_absent(item)
        return

    prepared_item = item if item.state_prepared else _prepare_terminal_replay_work_item(item)
    job.__dict__["_orca_terminal_replay_item"] = prepared_item
    if prepared_item.resolved_status != item.observed_status or (
        prepared_item.run_id and prepared_item.recorded_run_id != prepared_item.run_id
    ):
        updated = update_terminal(
            prepared_item.queue_root,
            prepared_item.queue_id,
            prepared_item.resolved_status,
            run_id=prepared_item.run_id,
            expected_task_id=prepared_item.task_id,
        )
        if not updated:
            logger.info(
                "Queue entry disappeared after terminal state preparation; "
                "continuing side effects: queue_id=%s",
                prepared_item.queue_id,
            )
        prepared_item = replace(
            prepared_item,
            recorded_run_id=prepared_item.run_id or prepared_item.recorded_run_id,
        )
        job.__dict__["_orca_terminal_replay_item"] = prepared_item

    _run_terminal_replay_side_effects(worker, prepared_item)
    _clear_terminal_replay_marker_or_confirm_absent(prepared_item)


def _pending_replay_state_is_superseded(item: TerminalReplayWorkItem) -> bool:
    if not str(item.reaction_dir or "").strip():
        return True
    if not item.task_id:
        return True
    current = load_state_generation_fingerprint(Path(item.reaction_dir).expanduser().resolve())
    if not current.readable:
        return False
    if current.readable and current.job_id == item.task_id:
        if (
            item.observed_state is not None
            and item.observed_state.readable
            and item.observed_state.job_id
            and item.observed_state.job_id != item.task_id
            and not current.terminal_status
        ):
            return True
        expected_run_id = str(item.run_id or item.recorded_run_id or "").strip()
        if (
            not expected_run_id
            and item.observed_state is not None
            and item.observed_state.job_id == item.task_id
        ):
            expected_run_id = item.observed_state.run_id
        return bool(expected_run_id and current.run_id and current.run_id != expected_run_id)
    if item.observed_state is not None:
        if not item.observed_state.readable:
            return False
        if current != item.observed_state:
            return True
        return bool(
            current.readable
            and current.job_id
            and current.job_id != item.task_id
            and not current.terminal_status
        )
    return bool(current.job_id and current.job_id != item.task_id)


def _reconcile_orphaned_running(worker: Any) -> None:
    recover_orphaned_engine_slots(worker.admission_root, strict=False)
    before_entries = queue_entries_with_roots(worker.cfg)
    before_by_key = {
        (str(Path(root).expanduser().resolve()), queue_entry_id(entry)): entry
        for root, entry in before_entries
    }
    replay_state = get_replay_state(worker)
    previous_statuses = replay_state.reconcile_statuses
    if previous_statuses is None:
        # Process startup has no observed status edge.  Treat the first queue
        # snapshot as the replay cursor instead of inventing RUNNING origins for
        # historical terminal rows.  A terminal row that really has unfinished
        # side effects remains replayable through its durable marker below, while
        # lifecycle reconciliation can still expose a real active -> terminal edge
        # between ``before_entries`` and ``after_entries`` in this same poll.
        previous_statuses = {
            key: normalized_entry_status(entry) for key, entry in before_by_key.items()
        }
    protected_queue_keys, protected_queue_ids = live_queue_slot_keys_for_slots(
        worker.admission_root,
        list_slots_fn=list_slots,
    )
    reconcile_orphaned_process_entries(
        worker,
        hooks=EngineQueueProcessReconcileHooks(
            queue_roots_fn=queue_roots,
            reconcile_stale_slots_fn=reconcile_stale_slots,
            reconcile_orphaned_running_entries_fn=reconcile_orphaned_running_entries,
            reconcile_orphaned_running_entries_kwargs={
                "ignore_worker_pid": True,
                "protected_queue_keys": protected_queue_keys,
                "protected_queue_ids": protected_queue_ids,
            },
        ),
    )
    # Reconciliation can terminalize a job whose original parent died, and an
    # old child can also honor cancellation directly. Replay the normal
    # terminal side effects idempotently so job-location records and one-shot
    # notifications are not lost with the parent process.
    after_entries = queue_entries_with_roots(worker.cfg)
    pending_replays = dict(replay_state.pending_replays)
    previously_blocked_markers = replay_state.blocked_marker_keys
    blocked_marker_keys: set[tuple[str, str]] = set()
    for queue_root, entry in after_entries:
        if normalized_entry_status(entry) not in TERMINAL_QUEUE_STATUSES:
            continue
        resolved_root = str(Path(queue_root).expanduser().resolve())
        key = (resolved_root, queue_entry_id(entry))
        marker_kind = terminal_replay_marker_kind(entry)
        if marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED:
            blocked_marker_keys.add(key)
            if key not in previously_blocked_markers:
                logger.error(
                    "ORCA terminal replay is repair-blocked by an invalid or unsupported "
                    "durable marker; retaining the queue generation from clear/force: "
                    "queue_id=%s queue_root=%s",
                    queue_entry_id(entry),
                    resolved_root,
                )
            continue
        if marker_kind is not TerminalReplayMarkerKind.VALID:
            continue
        reaction_key = reaction_generation_key(entry)
        if reaction_key is None:
            continue
        durable_item = new_terminal_replay_work_item(
            queue_root,
            entry,
            reaction_dir=queue_entry_reaction_dir(entry),
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(key)
        if not (
            isinstance(existing_item, TerminalReplayWorkItem)
            and existing_item.task_id == durable_item.task_id
            and existing_item.reaction_key == durable_item.reaction_key
        ):
            pending_replays[key] = durable_item
    replay_state.blocked_marker_keys = blocked_marker_keys

    superseded_replay_keys: set[tuple[str, str]] = set()
    for key, item in list(pending_replays.items()):
        if _pending_replay_state_is_superseded(item):
            # Revalidate prepared snapshots before owner selection even while the
            # terminal queue row still exists.  Otherwise a newer state identity
            # can make selection return ``None`` and strand the old snapshot in
            # the pending map forever.
            try:
                _clear_terminal_replay_marker(item)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to clear superseded ORCA terminal replay marker: %s",
                    item.queue_id,
                )
                continue
            pending_replays.pop(key, None)
            superseded_replay_keys.add(key)

    generation_rows: dict[str, list[ReactionGenerationRow]] = {}
    current_generation_keys: set[tuple[str, str]] = set()
    for queue_root, entry in after_entries:
        reaction_key = reaction_generation_key(entry)
        if reaction_key is None:
            continue
        resolved_root = str(Path(queue_root).expanduser().resolve())
        queue_id = queue_entry_id(entry)
        owner = (resolved_root, queue_id)
        before_entry = before_by_key.get(owner)
        before_status = normalized_entry_status(before_entry)
        pending_item = pending_replays.get(owner)
        pending_replay = isinstance(pending_item, TerminalReplayWorkItem)
        current_generation_keys.add(owner)
        marker_kind = terminal_replay_marker_kind(entry)
        if normalized_entry_status(entry) in TERMINAL_QUEUE_STATUSES and (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            # Administrative fences own no run artifact generation.  An
            # invalid/unsupported marker is likewise excluded so neither an
            # observed edge nor a stale in-memory snapshot can bypass its
            # repair-blocked boundary.
            pending_replays.pop(owner, None)
            continue
        generation_rows.setdefault(reaction_key, []).append(
            ReactionGenerationRow(
                owner=owner,
                task_id=str(queue_entry_task_id(entry) or "").strip(),
                status=normalized_entry_status(entry),
                # If state preparation failed after this generation was selected,
                # keep the observed active -> terminal edge with its immutable
                # snapshot.  The next poll otherwise sees a terminal -> terminal
                # row and can incorrectly hand ownership back to stale artifacts.
                # Once preparation succeeds, state identity becomes authoritative
                # and a mismatch correctly supersedes this pending replay.
                transitioned_from_active=(
                    before_status in ACTIVE_QUEUE_STATUSES
                    or (
                        isinstance(pending_item, TerminalReplayWorkItem)
                        and not pending_item.state_prepared
                    )
                ),
                pending_replay=pending_replay,
            )
        )
    for key, item in pending_replays.items():
        if key in current_generation_keys:
            continue
        generation_rows.setdefault(item.reaction_key, []).append(
            ReactionGenerationRow(
                owner=key,
                task_id=item.task_id,
                status=item.resolved_status or item.observed_status,
                pending_replay=True,
            )
        )

    previous_owners = replay_state.generation_owners
    previous_owner_active = replay_state.generation_owner_active
    latest_generation_by_reaction: dict[str, tuple[str, str]] = {}
    latest_owner_active: dict[str, bool] = {}
    for reaction_key, rows in generation_rows.items():
        previous_owner = previous_owners.get(reaction_key)
        selected_owner = _select_generation_owner(
            rows,
            previous_owner=previous_owner,
            previous_owner_was_active=bool(previous_owner_active.get(reaction_key, False)),
            artifacts=_load_artifact_generation(reaction_key),
        )
        if selected_owner is None:
            continue
        latest_generation_by_reaction[reaction_key] = selected_owner
        selected_row = next(row for row in rows if row.owner == selected_owner)
        latest_owner_active[reaction_key] = selected_row.active
    replay_state.generation_owners = latest_generation_by_reaction
    replay_state.generation_owner_active = latest_owner_active

    after_statuses: dict[tuple[str, str], str] = {}
    for queue_root, entry in after_entries:
        queue_id = queue_entry_id(entry)
        key = (str(Path(queue_root).expanduser().resolve()), queue_id)
        status = normalized_entry_status(entry)
        after_statuses[key] = status
        if status not in TERMINAL_QUEUE_STATUSES:
            pending_replays.pop(key, None)
            continue
        marker_kind = terminal_replay_marker_kind(entry)
        if (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            pending_replays.pop(key, None)
            continue
        if key in superseded_replay_keys:
            # The newer generation identity is definitive.  Advance the cursor
            # to this row's terminal status so it is not reconsidered forever.
            after_statuses[key] = status
            continue
        before_entry = before_by_key.get(key)
        before_status = normalized_entry_status(before_entry)
        # Replay requires positive evidence: either a durable replay marker (or
        # an in-memory retry snapshot), an active row terminalized during this
        # reconciliation, or an active status observed by the prior poll.  A
        # terminal row first seen after startup is closed history, not proof of a
        # transition; replaying it can rewrite its state/run identity and resend
        # an old notification.
        observed_active_transition = (
            before_status in ACTIVE_QUEUE_STATUSES
            or previous_statuses.get(key) in ACTIVE_QUEUE_STATUSES
        )
        if key not in pending_replays and not observed_active_transition:
            continue
        reaction_dir = queue_entry_reaction_dir(entry)
        if not reaction_dir:
            continue
        reaction_key = reaction_generation_key(entry) or ""
        if latest_generation_by_reaction.get(reaction_key) != key:
            logger.debug(
                "Skipping terminal replay for superseded or ambiguous ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                queue_id,
                reaction_dir,
            )
            # Ambiguity is retryable: state/report identity may become durable on
            # the next poll without another queue status transition.
            after_statuses[key] = "running"
            if reaction_key in latest_generation_by_reaction:
                pending_replays.pop(key, None)
            continue

        new_item = new_terminal_replay_work_item(
            queue_root,
            entry,
            reaction_dir=reaction_dir,
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(key)
        if isinstance(
            existing_item, TerminalReplayWorkItem
        ) and _pending_replay_state_is_superseded(existing_item):
            _clear_terminal_replay_marker(existing_item)
            pending_replays.pop(key, None)
            after_statuses[key] = status
            continue
        item = (
            existing_item
            if isinstance(existing_item, TerminalReplayWorkItem)
            and existing_item.reaction_key == new_item.reaction_key
            and existing_item.task_id == new_item.task_id
            else new_item
        )
        pending_replays[key] = item
        try:
            if not item.state_prepared:
                item = _prepare_terminal_replay_work_item(item)
                pending_replays[key] = item
            if item.resolved_status != status or (
                item.run_id and item.recorded_run_id != item.run_id
            ):
                updated = update_terminal(
                    item.queue_root,
                    item.queue_id,
                    item.resolved_status,
                    run_id=item.run_id,
                    expected_task_id=item.task_id,
                )
                if not updated:
                    logger.info(
                        "Queue entry disappeared after terminal state preparation; "
                        "continuing side effects: queue_id=%s",
                        item.queue_id,
                    )
            _run_terminal_replay_side_effects(worker, item)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to replay terminal side effects for reconciled ORCA job %s",
                queue_id,
            )
            # Keep this transition pending so the next periodic reconcile
            # retries the idempotent terminal side effects.
            after_statuses[key] = "running"
        else:
            try:
                _clear_terminal_replay_marker(item)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to clear completed ORCA terminal replay marker: %s",
                    item.queue_id,
                )
                after_statuses[key] = "running"
            else:
                pending_replays.pop(key, None)
                after_statuses[key] = item.resolved_status

    # A queue clear can remove the entry after state synthesis but before an
    # upsert/notification succeeds.  Retry from the immutable snapshot, including
    # a preparation that failed before the entry disappeared, but only after the
    # current owner and artifact generation are revalidated on every attempt.
    for key, item in list(pending_replays.items()):
        if key in current_generation_keys:
            continue
        if _pending_replay_state_is_superseded(item):
            logger.info(
                "Dropping terminal replay superseded by a newer ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                item.queue_id,
                item.reaction_dir,
            )
            pending_replays.pop(key, None)
            continue
        selected_owner = latest_generation_by_reaction.get(item.reaction_key)
        if selected_owner != key:
            if selected_owner is not None and selected_owner != key:
                pending_replays.pop(key, None)
            continue
        try:
            if not item.state_prepared:
                item = _prepare_terminal_replay_work_item(item)
                pending_replays[key] = item
            _run_terminal_replay_side_effects(worker, item)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to retry terminal side effects after queue entry disappeared: %s",
                item.queue_id,
            )
        else:
            pending_replays.pop(key, None)

    replay_state.pending_replays = pending_replays
    replay_state.reconcile_statuses = after_statuses


def reconcile_worker_state(worker: Any) -> None:
    _reconcile_orphaned_running(worker)


def _load_state_for_terminal_generation(
    job_dir: Path,
    *,
    expected_job_id: str,
    observed_state: StateGenerationFingerprint | None = None,
) -> RunState | None:
    state_file = state_path(job_dir)
    state_existed = state_file.exists()
    state = load_state(job_dir)
    if (state_existed or state_file.exists()) and state is None:
        raise RuntimeError(f"ORCA run state is unreadable: {state_file}")
    if state is None:
        current_fingerprint = StateGenerationFingerprint(present=False, readable=True)
        if observed_state is not None and current_fingerprint != observed_state:
            raise RuntimeError(
                "ORCA terminal replay state disappeared after its queue mark: "
                f"job_dir={job_dir} expected_job_id={expected_job_id}"
            )
        return None
    if not expected_job_id:
        return state

    state_job_id = worker_tracking.payload_job_id(state)
    existing_terminal_status = terminal_status_from_run_state(state)
    if state_job_id == expected_job_id:
        if (
            observed_state is not None
            and observed_state.readable
            and observed_state.job_id
            and observed_state.job_id != expected_job_id
            and existing_terminal_status is None
        ):
            raise RuntimeError(
                "ORCA terminal replay observed a new run for the expected task "
                "after marking a different state generation: "
                f"job_dir={job_dir} expected_job_id={expected_job_id} "
                f"observed_job_id={observed_state.job_id}"
            )
        if (
            observed_state is not None
            and observed_state.readable
            and observed_state.job_id == expected_job_id
            and observed_state.run_id
            and str(state.get("run_id") or "").strip()
            and str(state.get("run_id") or "").strip() != observed_state.run_id
        ):
            raise RuntimeError(
                "ORCA terminal replay observed a newer run for the same task: "
                f"job_dir={job_dir} expected_job_id={expected_job_id}"
            )
        return state

    if observed_state is not None:
        current_fingerprint = StateGenerationFingerprint(
            present=True,
            readable=True,
            job_id=state_job_id,
            run_id=str(state.get("run_id") or "").strip(),
            terminal_status=existing_terminal_status or "",
        )
        if current_fingerprint != observed_state:
            raise RuntimeError(
                "ORCA terminal replay was superseded by another state generation: "
                f"job_dir={job_dir} expected_job_id={expected_job_id} "
                f"state_job_id={state_job_id or '<missing>'}"
            )
        if state_job_id and state_job_id != expected_job_id and not existing_terminal_status:
            raise RuntimeError(
                "ORCA terminal replay observed another active state generation: "
                f"job_dir={job_dir} expected_job_id={expected_job_id} "
                f"state_job_id={state_job_id}"
            )
    if existing_terminal_status is not None:
        # A forced submission can reuse a reaction directory before the new
        # child writes state.  A complete terminal result is durable evidence
        # that this is the previous generation, so synthesize a fresh state for
        # the expected queue task instead of relabeling the old result.
        logger.info(
            "Ignoring previous-generation terminal ORCA state: "
            "job_dir=%s expected_job_id=%s state_job_id=%s",
            job_dir,
            expected_job_id,
            state_job_id,
        )
        return None

    # A nonterminal mismatch can be the current active generation.  Failing
    # closed under the run lock is essential: overwriting it would make an old
    # finalizer publish a terminal result for a newer child.
    raise RuntimeError(
        "ORCA run state belongs to a different active generation: "
        f"job_dir={job_dir} expected_job_id={expected_job_id} "
        f"state_job_id={state_job_id or '<missing>'}"
    )


def record_cancelled_run_state(
    job_dir: Path,
    *,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    observed_state: StateGenerationFingerprint | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Write a terminal "cancelled" run state for an interrupted run.

    A cancelled run is stopped by a signal and never writes its own terminal
    result, so the run state lingers as ``running``. That leaves a stale run
    snapshot in the activity list and starves the terminal notification
    (which requires ``final_result``). Persist a cancelled outcome here.

    Returns ``(run_id, terminal_status)``: the run_id when known (so the queue
    entry can be matched to this snapshot) and the terminal status now recorded
    in the run state -- "cancelled" when we wrote it, or a pre-existing terminal
    status we refused to clobber. When no state exists yet, a minimal cancelled
    state is created from the queue identity so indexing cannot fall back to
    ``unknown``.
    """
    expected_job_id = str(fallback_job_id or "").strip()
    with acquire_run_lock(job_dir):
        state = _load_state_for_terminal_generation(
            job_dir,
            expected_job_id=expected_job_id,
            observed_state=observed_state,
        )
        if state is None:
            selected_text = str(selected_inp or "").strip()
            selected_path = Path(selected_text).expanduser() if selected_text else job_dir / "-"
            if not selected_path.is_absolute():
                selected_path = job_dir / selected_path
            state = new_state(job_dir, selected_path, max_retries=0)
            if expected_job_id:
                state["job_id"] = expected_job_id
        if execution_provenance:
            state["execution_provenance"] = dict(execution_provenance)
        run_id = str(state.get("run_id") or "").strip() or None
        final_result = state.get("final_result")
        if isinstance(final_result, dict):
            existing_status = str(final_result.get("status") or "").strip()
            if existing_status in {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED}:
                # A real terminal outcome was already recorded (e.g. the run finished
                # just before cancellation landed); do not clobber it, and report the
                # real status so the queue entry is reconciled to what actually
                # happened instead of being mislabeled "cancelled".
                finalize_state(
                    job_dir,
                    state,
                    status=existing_status,
                    final_result=final_result,
                )
                write_report_files(job_dir, state)
                return run_id, existing_status
        cancelled_result = build_final_result(
            status=STATUS_CANCELLED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="cancel_requested",
            last_out_path=last_out_path_from_state(state),
        )
        finalize_state(job_dir, state, status=STATUS_CANCELLED, final_result=cancelled_result)
        write_report_files(job_dir, state)
        return run_id, STATUS_CANCELLED


def record_failed_run_state(
    job_dir: Path,
    *,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    reason: str,
    observed_state: StateGenerationFingerprint | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Ensure an exited child has a terminal state for its queue generation."""

    expected_job_id = str(fallback_job_id or "").strip()
    with acquire_run_lock(job_dir):
        state = _load_state_for_terminal_generation(
            job_dir,
            expected_job_id=expected_job_id,
            observed_state=observed_state,
        )
        if state is None:
            selected_text = str(selected_inp or "").strip()
            selected_path = Path(selected_text).expanduser() if selected_text else job_dir / "-"
            if not selected_path.is_absolute():
                selected_path = job_dir / selected_path
            state = new_state(job_dir, selected_path, max_retries=0)
            if expected_job_id:
                state["job_id"] = expected_job_id
        if execution_provenance:
            state["execution_provenance"] = dict(execution_provenance)
        run_id = str(state.get("run_id") or "").strip() or None
        final_result = state.get("final_result")
        if isinstance(final_result, dict):
            existing_status = str(final_result.get("status") or "").strip()
            if existing_status in {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED}:
                finalize_state(
                    job_dir,
                    state,
                    status=existing_status,
                    final_result=final_result,
                )
                write_report_files(job_dir, state)
                return run_id, existing_status
        failed_result = build_final_result(
            status=STATUS_FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason=reason,
            last_out_path=last_out_path_from_state(state),
        )
        finalize_state(job_dir, state, status=STATUS_FAILED, final_result=failed_result)
        write_report_files(job_dir, state)
        return run_id, STATUS_FAILED


__all__ = [
    "OrcaWorkerReplayState",
    "RunningJob",
    "finalize_child_exit",
    "finalize_completed_job",
    "handle_worker_start_error",
    "job_pending_replay_item",
    "job_queue_root",
    "shutdown_running_job",
    "new_terminal_replay_work_item",
    "normalized_entry_status",
    "on_worker_process_started",
    "orca_worker_lifecycle_hooks",
    "reaction_generation_key",
    "reconcile_worker_state",
    "record_cancelled_run_state",
    "get_replay_state",
    "strictly_finish_terminal_replay",
    "terminal_replay_blocks_new_generation",
]
