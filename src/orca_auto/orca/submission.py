"""Submit an ORCA input directory to the durable queue.

``create_queued_submission`` runs the staged pipeline (prepare inputs, build
the execution snapshot, validate its intent, assemble queue metadata, publish
the row with its job record, own the snapshot);
``submit_reaction_dir_to_queue`` wraps it for the CLI with conflict detection
and one-line failure reporting. The smaller helpers here (worker status,
queue metadata, job-record projection) are shared with the queue
worker and the CLI status views.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.paths.retired import path_is_retired_workflow_owned
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    mark_snapshot_intent_owned,
    transition_snapshot_intent,
)
from orca_auto.core.queue.persistence import QueueStoreCorruptError
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.store import QueueAfterCommitError
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils.persistence import timestamped_token
from orca_auto.orca.queue.enqueue_publication import (
    EnqueuePublicationOutcome,
    EnqueuePublicationOutcomeUnknown,
    EnqueuePublicationSpec,
    run_enqueue_publication,
)
from orca_auto.orca.run_dir_guard import (
    active_run_dir_pinned_target,
    assert_run_dir_publication_allowed,
)

from .config import load_config
from .execution import active_direct_run_error, select_latest_inp
from .execution_binding import (
    build_orca_execution_snapshot,
    cleanup_unowned_orca_execution_snapshot,
)
from .inp_rewriter import prepare_submission_resource_request
from .input_artifacts import OrcaSelectedInputArtifacts, selected_input_artifacts
from .job_locations import resolve_job_metadata
from .queue import adapter as queue_adapter
from .queue.adapter import DuplicateEntryError
from .queue.entries import queue_entry_is_retired_workflow_owned
from .queue.job_records import upsert_queued_job_record
from .queue.notifications import QUEUED_NOTIFICATION_PENDING_KEY
from .queue.orphans import DeadRunningRowUnjudgeableError, read_worker_pid
from .resource_directives import PreparedSubmissionResourceInput
from .run_context import WorkerStatusInfo, resolve_submission_context

logger = logging.getLogger(__name__)


def _snapshot_cleanup_job_dir(reaction_dir: Path, snapshot: Any) -> Path:
    identity = snapshot.get("job_dir_identity") if isinstance(snapshot, dict) else None
    if not isinstance(identity, dict):
        return reaction_dir
    expected = (
        int(identity.get("device", -1)),
        int(identity.get("inode", -1)),
    )
    candidates = (reaction_dir, active_run_dir_pinned_target())
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            candidate_stat = candidate.stat()
        except OSError:
            continue
        if (int(candidate_stat.st_dev), int(candidate_stat.st_ino)) == expected:
            return candidate
    raise ValueError("ORCA cleanup target no longer matches the execution snapshot")


def mark_orca_snapshot_owned(
    intent_root: Path,
    intent_token: str,
) -> str | None:
    return mark_snapshot_intent_owned(
        intent_root,
        intent_token,
        intent_label="queued ORCA snapshot",
    )


@dataclass(frozen=True)
class QueuedSubmissionResult:
    entry: Any
    reaction_dir: Path
    selected_inp: Path | None
    queue_metadata: dict[str, Any]
    worker_info: WorkerStatusInfo


@dataclass(frozen=True)
class DirectQueueSubmission:
    status: str
    reason: str = ""
    stderr: str = ""
    context: Any | None = None
    queued_result: Any | None = None


class QueuePublicationCancelledError(RuntimeError):
    """Raised when cancellation revokes publication before its side effects."""


def active_queue_entry(allowed_root: Path, reaction_dir: Path) -> QueueEntry | None:
    return queue_adapter.get_active_entry_for_reaction_dir(allowed_root, str(reaction_dir))


def find_submission_conflict(
    allowed_root: Path,
    reaction_dir: Path,
) -> str | None:
    active_entry = active_queue_entry(allowed_root, reaction_dir)
    if active_entry is not None:
        return (
            "Job directory already queued: "
            f"{reaction_dir} (queue_id={queue_adapter.queue_entry_id(active_entry)}, "
            f"status={queue_adapter.queue_entry_status(active_entry)})"
        )
    return active_direct_run_error(reaction_dir, logger=logger)


def worker_status_for_submission(allowed_root: Path) -> WorkerStatusInfo:
    pid = read_worker_pid(allowed_root)
    if pid is None:
        return WorkerStatusInfo(status="inactive")
    return WorkerStatusInfo(status="running", pid=pid)


def queue_entry_worker_log(entry: Any) -> Any | None:
    metadata = queue_adapter.queue_entry_metadata(entry)
    worker_log = metadata.get("worker_log")
    if isinstance(worker_log, (str, Path)):
        return worker_log
    return None


def worker_status_with_log_file(
    worker_info: WorkerStatusInfo,
    worker_log: Any | None,
) -> WorkerStatusInfo:
    return WorkerStatusInfo(
        status=worker_info.status,
        pid=worker_info.pid,
        log_file=worker_log or worker_info.log_file,
        detail=worker_info.detail,
    )


def prepared_resource_input_from_selected_inp(
    cfg: Any,
    selected_inp: Path | None,
    *,
    logger: logging.Logger,
) -> PreparedSubmissionResourceInput:
    if selected_inp is None:
        raise ValueError("No .inp file selected for ORCA queue submission.")
    prepared = prepare_submission_resource_request(
        selected_inp,
        default_max_cores=int(cfg.resources.max_cores_per_task),
        default_max_memory_gb=int(cfg.resources.max_memory_gb_per_task),
    )
    if prepared.actions:
        logger.info(
            "Prepared private ORCA input resource directives for %s: %s",
            selected_inp,
            ", ".join(prepared.actions),
        )
    return prepared


def warn_ignored_resource_override_flags(args: Any, *, logger: logging.Logger) -> None:
    if getattr(args, "max_cores", None) is None and getattr(args, "max_memory_gb", None) is None:
        return
    logger.warning(
        "Standalone ORCA queue submission ignores --max-cores/--max-memory-gb; "
        "resource metadata is read from the input file."
    )


def build_queue_metadata(
    *,
    artifacts: OrcaSelectedInputArtifacts,
    job_type: str,
    molecule_key: str,
    resource_request: Mapping[str, int],
    execution_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Assemble queue values from an already-created snapshot without filesystem work."""
    metadata: dict[str, Any] = {
        "submitted_via": "run_inp",
        QUEUED_NOTIFICATION_PENDING_KEY: True,
        "job_type": job_type,
        "molecule_key": molecule_key,
        "resource_request": dict(resource_request),
        "resource_actual": dict(resource_request),
    }
    if artifacts.selected_inp:
        metadata["source_selected_inp"] = artifacts.selected_inp
        metadata["selected_inp"] = execution_snapshot["selected_inp"]
        metadata["selected_input_path"] = artifacts.selected_input_path
    metadata["selected_input_xyz"] = artifacts.selected_input_xyz
    metadata["execution_snapshot"] = execution_snapshot
    return metadata


def worker_status_with_detail(
    worker_info: WorkerStatusInfo,
    detail: str | None,
) -> WorkerStatusInfo:
    if not detail:
        return worker_info
    worker_detail = worker_info.detail
    if worker_detail:
        worker_detail = f"{worker_detail}; {detail}"
    else:
        worker_detail = detail
    return WorkerStatusInfo(
        status=worker_info.status,
        pid=worker_info.pid,
        log_file=worker_info.log_file,
        detail=worker_detail,
    )


@dataclass(frozen=True)
class _PreparedSubmissionInputs:
    """What the input-preparation stage settled before any snapshot exists."""

    selected_inp: Path
    artifacts: OrcaSelectedInputArtifacts
    job_type: str
    molecule_key: str
    prepared_input: PreparedSubmissionResourceInput
    priority: int
    force: bool


@dataclass(frozen=True)
class _SnapshotIntent:
    """The snapshot-intent ledger entry a freshly built snapshot points at."""

    root: Path
    token: str


def _submission_queue_root(cfg: Any, reaction_dir: Path) -> Path:
    """Stage 0: the queue root, after refusing retired-workflow targets."""
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    resolved_reaction_dir = reaction_dir.expanduser().resolve()
    if path_is_retired_workflow_owned(resolved_reaction_dir, allowed_root) or any(
        queue_entry_is_retired_workflow_owned(entry, allowed_root)
        and queue_adapter.queue_entry_reaction_dir(entry)
        and resolved_reaction_dir.is_relative_to(
            Path(queue_adapter.queue_entry_reaction_dir(entry)).expanduser().resolve()
        )
        for entry in queue_adapter.list_queue(allowed_root)
    ):
        raise ValueError(
            "Workflow directories are retired; submit a standalone ORCA input directory"
        )
    return allowed_root


def _prepare_submission_inputs(
    cfg: Any,
    args: Any,
    reaction_dir: Path,
    *,
    selected_inp: Path | None,
) -> _PreparedSubmissionInputs:
    """Stage 1: select and inspect the input; nothing durable is written yet."""
    if selected_inp is None:
        try:
            selected_inp = select_latest_inp(reaction_dir)
        except ValueError:
            selected_inp = None
    warn_ignored_resource_override_flags(args, logger=logger)
    priority = normalize_queue_priority(getattr(args, "priority", 10))
    force = bool(getattr(args, "force", False))
    assert_run_dir_publication_allowed("ORCA target mutation preflight")
    artifacts = selected_input_artifacts(selected_inp)
    job_type, molecule_key = resolve_job_metadata(artifacts.selected_inp, reaction_dir)
    prepared_input = prepared_resource_input_from_selected_inp(cfg, selected_inp, logger=logger)
    assert selected_inp is not None
    return _PreparedSubmissionInputs(
        selected_inp=selected_inp,
        artifacts=artifacts,
        job_type=job_type,
        molecule_key=molecule_key,
        prepared_input=prepared_input,
        priority=priority,
        force=force,
    )


def _build_execution_snapshot(
    cfg: Any,
    reaction_dir: Path,
    inputs: _PreparedSubmissionInputs,
    *,
    queue_root: Path,
) -> dict[str, Any]:
    """Stage 2: materialize the immutable generation and its CREATING intent."""
    return build_orca_execution_snapshot(
        reaction_dir,
        inputs.selected_inp,
        selected_input_xyz=inputs.artifacts.selected_input_xyz,
        resource_request=inputs.prepared_input.resource_request,
        orca_executable=cfg.paths.orca_executable,
        queue_root=queue_root,
        snapshot_intent_token=timestamped_token("snapshot_intent", token_bytes=16),
        normalized_selected_payload=inputs.prepared_input.normalized_payload,
        source_selected_sha256=inputs.prepared_input.source_sha256,
    )


def _validated_snapshot_intent(execution_snapshot: Any, *, queue_root: Path) -> _SnapshotIntent:
    """Stage 3: the snapshot must name an intent under this queue root."""
    if not isinstance(execution_snapshot, dict):
        raise RuntimeError("ORCA submission has no execution snapshot")
    intent_root = (
        Path(str(execution_snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or ""))
        .expanduser()
        .resolve()
    )
    intent_token = str(execution_snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    if intent_root != queue_root or not intent_token:
        raise RuntimeError("ORCA submission snapshot intent does not match its queue root")
    return _SnapshotIntent(root=intent_root, token=intent_token)


def _assemble_queue_metadata(
    reaction_dir: Path,
    inputs: _PreparedSubmissionInputs,
    execution_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Stage 4: the queue row's metadata, stamped with the job directory."""
    queue_metadata = build_queue_metadata(
        artifacts=inputs.artifacts,
        job_type=inputs.job_type,
        molecule_key=inputs.molecule_key,
        resource_request=inputs.prepared_input.resource_request,
        execution_snapshot=execution_snapshot,
    )
    # Post-commit recovery matches the same job directory stamped by the adapter.
    queue_metadata["reaction_dir"] = str(reaction_dir.expanduser().resolve())
    return queue_metadata


def _cleanup_submission_snapshot(reaction_dir: Path, execution_snapshot: Any) -> None:
    """Compensation: remove a generation the queue never took ownership of."""
    cleanup_unowned_orca_execution_snapshot(
        _snapshot_cleanup_job_dir(reaction_dir, execution_snapshot),
        execution_snapshot,
    )


def _publish_submission(
    cfg: Any,
    reaction_dir: Path,
    inputs: _PreparedSubmissionInputs,
    *,
    queue_root: Path,
    task_id: str,
    queue_metadata: dict[str, Any],
    execution_snapshot: dict[str, Any],
) -> EnqueuePublicationOutcome:
    """Commit the row and publish its location record before completing the lease."""

    def publish(current: QueueEntry) -> None:
        upsert_queued_job_record(cfg, current)

    def mark_failed_via_adapter(root: Path, queue_id: str, **kwargs: Any) -> Any:
        # The adapter's mark_failed installs the administrative fence-only
        # replay marker, keeping the fenced generation's terminal ownership.
        return queue_adapter.mark_failed(
            root,
            queue_id,
            publish_terminal_side_effects=False,
            **kwargs,
        )

    def enqueue_via_adapter(root: Path, **kwargs: Any) -> QueueEntry:
        return queue_adapter.enqueue(
            root,
            str(reaction_dir),
            priority=kwargs["priority"],
            force=inputs.force,
            task_id=kwargs["task_id"],
            task_kind=kwargs["task_kind"],
            metadata=kwargs["metadata"],
            before_commit_fn=kwargs.get("before_commit_fn"),
            after_commit_fn=kwargs.get("after_commit_fn"),
            admission_root=Path(cfg.runtime.resolved_admission_root),
        )

    spec = EnqueuePublicationSpec(
        queue_root=queue_root,
        app_name=queue_adapter.QUEUE_APP_NAME,
        task_id=task_id,
        task_kind=queue_adapter.QUEUE_TASK_KIND,
        engine=queue_adapter.QUEUE_ENGINE,
        priority=inputs.priority,
        metadata=queue_metadata,
        label="ORCA",
        publish=publish,
        before_commit_fn=lambda: assert_run_dir_publication_allowed(
            "ORCA durable queue pre-commit"
        ),
        after_commit_fn=lambda: assert_run_dir_publication_allowed(
            "ORCA durable queue post-commit"
        ),
        enqueue_fn=enqueue_via_adapter,
        mark_failed_fn=mark_failed_via_adapter,
        # Ambiguity-fenced rows keep the administrative fence-only marker so a
        # successor generation stays blocked until the duplicates are cleared.
        ambiguous_fence_metadata={queue_adapter.TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY: True},
        on_compensated_failure=lambda: _cleanup_submission_snapshot(
            reaction_dir, execution_snapshot
        ),
        job_dir_metadata_key="reaction_dir",
        same_generation=queue_adapter.queue_entries_same_publication_generation,
    )
    return run_enqueue_publication(spec)


def _submission_worker_info(
    queue_root: Path, entry: Any, warnings: list[str | None]
) -> WorkerStatusInfo:
    """The worker status reported back to the submitter, with every warning."""
    worker_info = worker_status_with_log_file(
        worker_status_for_submission(queue_root),
        queue_entry_worker_log(entry),
    )
    for warning in warnings:
        worker_info = worker_status_with_detail(worker_info, warning)
    return worker_info


def create_queued_submission(
    cfg: Any,
    args: Any,
    reaction_dir: Path,
    *,
    selected_inp: Path | None = None,
) -> QueuedSubmissionResult:
    """Submit one ORCA input directory to the durable queue.

    Stages, in order: prepare inputs, build the execution snapshot, validate
    its intent, assemble the queue metadata, publish the row and job record,
    then take ownership of the snapshot. The parent worker owns queued delivery.
    A failure between snapshot creation and publication removes the unowned generation;
    a compensated publication failure removes it through the driver.
    """
    queue_root = _submission_queue_root(cfg, reaction_dir)
    inputs = _prepare_submission_inputs(cfg, args, reaction_dir, selected_inp=selected_inp)
    execution_snapshot = _build_execution_snapshot(cfg, reaction_dir, inputs, queue_root=queue_root)
    try:
        intent = _validated_snapshot_intent(execution_snapshot, queue_root=queue_root)
        queue_metadata = _assemble_queue_metadata(reaction_dir, inputs, execution_snapshot)
        task_id = timestamped_token("orca", token_bytes=16)
        transition_snapshot_intent(
            intent.root,
            intent.token,
            target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
            expected_states={SNAPSHOT_INTENT_STATE_CREATING},
        )
    except BaseException:
        _cleanup_submission_snapshot(reaction_dir, execution_snapshot)
        raise
    outcome = _publish_submission(
        cfg,
        reaction_dir,
        inputs,
        queue_root=queue_root,
        task_id=task_id,
        queue_metadata=queue_metadata,
        execution_snapshot=execution_snapshot,
    )
    entry = outcome.entry
    marker_warning = mark_orca_snapshot_owned(intent.root, intent.token)
    if outcome.cancelled:
        raise QueuePublicationCancelledError(
            f"ORCA queue entry was cancelled before publication: {entry.queue_id}"
        )
    worker_info = _submission_worker_info(
        queue_root,
        entry,
        [marker_warning, *outcome.warnings],
    )
    return QueuedSubmissionResult(
        entry=entry,
        reaction_dir=reaction_dir,
        selected_inp=inputs.selected_inp,
        queue_metadata=queue_metadata,
        worker_info=worker_info,
    )


def submit_reaction_dir_to_queue(
    args: Any,
) -> DirectQueueSubmission:
    try:
        context = resolve_submission_context(
            args,
            cfg=None,
            load_config_fn=load_config,
            select_latest_inp_fn=select_latest_inp,
            logger=logger,
        )
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        # A missing, unreadable or invalid config is reported like every other
        # submission failure: one message, no traceback.
        return DirectQueueSubmission(
            status="failed",
            reason="invalid_config",
            stderr=str(exc),
        )
    if context is None:
        return DirectQueueSubmission(
            status="failed",
            reason="invalid_submission_target",
            stderr="failed to resolve ORCA submission target",
        )

    try:
        conflict_error = find_submission_conflict(
            context.allowed_root,
            context.reaction_dir,
        )
    except QueueStoreCorruptError as exc:
        return DirectQueueSubmission(
            status="failed",
            reason="queue_store_corrupt",
            stderr=str(exc),
            context=context,
        )
    if conflict_error is not None:
        return DirectQueueSubmission(
            status="failed",
            reason="submission_conflict",
            stderr=conflict_error,
            context=context,
        )

    try:
        queued = create_queued_submission(
            context.cfg,
            args,
            context.reaction_dir,
            selected_inp=context.selected_inp,
        )
    except (DuplicateEntryError, DeadRunningRowUnjudgeableError) as exc:
        # Both are this directory's own queue row standing in the way: an
        # active duplicate, or a dead RUNNING row whose slot protection cannot
        # be read. The message carries the hint; no traceback.
        return DirectQueueSubmission(
            status="failed",
            reason="submission_conflict",
            stderr=str(exc),
            context=context,
        )
    except ValueError as exc:
        # e.g. a reaction dir without any .inp: fail cleanly instead of
        # leaking a traceback through the CLI.
        return DirectQueueSubmission(
            status="failed",
            reason="invalid_submission_input",
            stderr=str(exc),
            context=context,
        )
    except QueuePublicationCancelledError as exc:
        return DirectQueueSubmission(
            status="failed",
            reason="submission_cancelled",
            stderr=str(exc),
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        reason = (
            "queue_enqueue_outcome_unknown"
            if isinstance(exc, EnqueuePublicationOutcomeUnknown)
            or (isinstance(exc, QueueAfterCommitError) and not exc.compensation_succeeded)
            else "queue_submission_failed"
        )
        return DirectQueueSubmission(
            status="failed",
            reason=reason,
            stderr=f"{exc.__class__.__name__}: {exc}",
            context=context,
        )
    return DirectQueueSubmission(status="submitted", context=context, queued_result=queued)
