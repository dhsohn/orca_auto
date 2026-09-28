"""Submit an ORCA input directory to the durable queue.

``submit_reaction_dir_to_queue`` is the ``run-dir`` entry point: it resolves
the target (job directory, newest input) using the command's loaded config,
refuses a directory that is already queued or running, and reports every
submission failure as one reason code and
message. ``create_queued_submission`` runs the staged pipeline: read the
selected input once and derive the queue metadata and resources from those
bytes, build the execution snapshot from the same bytes under a fresh intent,
publish the row with its job record, then own the snapshot.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.confined_io import read_stable_regular_file, require_confined_regular_file
from orca_auto.core.paths import is_subpath
from orca_auto.core.queue.persistence import QueueStoreCorruptError
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    mark_snapshot_intent_owned,
    transition_snapshot_intent,
)
from orca_auto.core.queue.store import QueueAfterCommitError
from orca_auto.core.queue.worker.pid_file import read_worker_pid_file
from orca_auto.core.utils.persistence import timestamped_token
from orca_auto.orca.queue.enqueue_publication import (
    EnqueuePublicationOutcomeUnknown,
    run_enqueue_publication,
)
from orca_auto.orca.run_dir_guard import (
    active_run_dir_pinned_target,
    assert_run_dir_publication_allowed,
)

from .config import AppConfig
from .execution_binding import (
    build_orca_execution_snapshot,
    cleanup_unowned_orca_execution_snapshot,
    same_directory_identity,
)
from .input_artifacts import OrcaSelectedInputArtifacts, xyzfile_input_path
from .input_syntax import orca_route_lines
from .job_type import job_type_from_routes
from .molecule_key import molecule_key_from_text
from .queue import adapter as queue_adapter
from .queue import entries as queue_entries
from .queue.adapter import DuplicateEntryError
from .queue.orphans import DeadRunningRowUnjudgeableError
from .resource_directives import (
    PreparedSubmissionResourceInput,
    prepare_submission_resource_request,
)
from .run_lock import run_lock_conflict_message

logger = logging.getLogger(__name__)

ORCA_GENERATED_INP_RE = re.compile(
    r"\.(scfgrad|scfhess|cis|autoci|cipsi|mrci|mdci|eprnmr|loc|nbo|compound|hess)"
    r"$",
    re.IGNORECASE,
)


def _snapshot_cleanup_job_dir(reaction_dir: Path, snapshot: Any) -> Path:
    identity = snapshot.get("job_dir_identity") if isinstance(snapshot, dict) else None
    if not isinstance(identity, dict):
        return reaction_dir
    candidates = (reaction_dir, active_run_dir_pinned_target())
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            candidate_stat = candidate.stat()
        except OSError:
            continue
        if same_directory_identity(candidate_stat, identity):
            return candidate
    raise ValueError("ORCA cleanup target no longer matches the execution snapshot")


@dataclass(frozen=True)
class WorkerStatusInfo:
    status: str | None = None
    pid: int | None = None
    log_file: str | Path | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SubmissionTarget:
    cfg: AppConfig
    reaction_dir: Path
    selected_inp: Path
    allowed_root: Path


@dataclass(frozen=True)
class QueuedSubmissionResult:
    entry: Any
    worker_info: WorkerStatusInfo


@dataclass(frozen=True)
class DirectQueueSubmission:
    status: str
    reason: str = ""
    stderr: str = ""
    target: SubmissionTarget | None = None
    queued_result: QueuedSubmissionResult | None = None


class QueuePublicationCancelledError(RuntimeError):
    """Raised when cancellation revokes publication before its side effects."""


def select_latest_inp(reaction_dir: Path) -> Path:
    resolved_reaction_dir = reaction_dir.expanduser().resolve()
    all_candidates = list(reaction_dir.glob("*.inp"))
    if not all_candidates:
        raise ValueError(f"No .inp file found in: {reaction_dir}")
    # Prefer user-authored base inputs over generated intermediate files.
    candidates = [p for p in all_candidates if not ORCA_GENERATED_INP_RE.search(p.stem)]
    if not candidates:
        candidates = all_candidates
    candidates = [
        require_confined_regular_file(
            resolved_reaction_dir,
            candidate,
            label="ORCA selected input",
        )
        for candidate in candidates
    ]
    candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name.lower()), reverse=True)
    if len(candidates) > 1:
        logger.warning(
            "Multiple ORCA .inp candidates found in %s; selected newest input %s",
            reaction_dir,
            candidates[0].name,
        )
    return candidates[0]


def resolve_submission_target(args: Any, *, cfg: AppConfig) -> SubmissionTarget | None:
    """Validate the job directory and select its input using the command's config.

    A refused directory or input is logged and returns None.
    """
    raw = getattr(args, "path", None)
    if not isinstance(raw, str) or not raw.strip():
        logger.error("job directory path is required")
        return None
    try:
        reaction_dir = Path(raw).expanduser().resolve()
        if not reaction_dir.exists() or not reaction_dir.is_dir():
            raise ValueError(f"Job directory not found: {reaction_dir}")
        allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
        if not is_subpath(reaction_dir, allowed_root):
            raise ValueError(
                f"Job directory must be under allowed root: {allowed_root}. got={reaction_dir}"
            )
        selected_inp = select_latest_inp(reaction_dir)
    except ValueError as exc:
        logger.error("%s", exc)
        return None
    return SubmissionTarget(
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        allowed_root=allowed_root,
    )


def find_submission_conflict(
    allowed_root: Path,
    reaction_dir: Path,
) -> str | None:
    active_entry = queue_adapter.get_active_entry_for_reaction_dir(allowed_root, str(reaction_dir))
    if active_entry is not None:
        return (
            "Job directory already queued: "
            f"{reaction_dir} (queue_id={active_entry.queue_id}, "
            f"status={active_entry.status.value})"
        )
    return run_lock_conflict_message(reaction_dir)


def build_queue_metadata(
    *,
    reaction_dir: Path,
    artifacts: OrcaSelectedInputArtifacts,
    job_type: str,
    molecule_key: str,
    resource_request: Mapping[str, int],
    execution_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Assemble queue values from an already-created snapshot without filesystem work."""
    return {
        "submitted_via": "run_inp",
        queue_entries.QUEUED_NOTIFICATION_PENDING_KEY: True,
        "job_type": job_type,
        "molecule_key": molecule_key,
        "resource_request": dict(resource_request),
        "resource_actual": dict(resource_request),
        "source_selected_inp": artifacts.selected_inp,
        "selected_inp": execution_snapshot["selected_inp"],
        "selected_input_path": artifacts.selected_input_path,
        "selected_input_xyz": artifacts.selected_input_xyz,
        "execution_snapshot": execution_snapshot,
        # Post-commit recovery matches the same job directory the adapter stamps.
        "reaction_dir": str(reaction_dir.expanduser().resolve()),
    }


@dataclass(frozen=True)
class _PreparedSubmissionInputs:
    """What the input-preparation stage settled before any snapshot exists."""

    selected_inp: Path
    source_payload: bytes
    artifacts: OrcaSelectedInputArtifacts
    job_type: str
    molecule_key: str
    prepared_input: PreparedSubmissionResourceInput
    priority: int
    force: bool


def _prepare_submission_inputs(
    cfg: Any, args: Any, selected_inp: Path
) -> _PreparedSubmissionInputs:
    """Stage 1: read the selected input once and derive everything from its bytes.

    Nothing durable is written yet. Queue metadata, the snapshot digests and
    the bound copy all describe these bytes, even when the file changes while
    the submission runs.
    """
    priority = normalize_queue_priority(getattr(args, "priority", None))
    force = bool(getattr(args, "force", False))
    assert_run_dir_publication_allowed("ORCA target mutation preflight")
    source_payload = read_stable_regular_file(selected_inp)
    text = source_payload.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    inp_path = selected_inp.expanduser().resolve()
    artifacts = OrcaSelectedInputArtifacts(
        selected_inp=str(selected_inp),
        selected_input_xyz=xyzfile_input_path(lines, inp_path.parent),
    )
    job_type = job_type_from_routes(orca_route_lines(lines))
    molecule_key = molecule_key_from_text(text, inp_path).key
    prepared_input = prepare_submission_resource_request(
        selected_inp,
        source_payload,
        default_max_cores=int(cfg.resources.max_cores_per_task),
        default_max_memory_gb=int(cfg.resources.max_memory_gb_per_task),
    )
    if prepared_input.actions:
        logger.info(
            "Prepared private ORCA input resource directives for %s: %s",
            selected_inp,
            ", ".join(prepared_input.actions),
        )
    return _PreparedSubmissionInputs(
        selected_inp=selected_inp,
        source_payload=source_payload,
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
    intent_token: str,
) -> dict[str, Any]:
    """Stage 2: materialize the immutable generation and its CREATING intent."""
    return build_orca_execution_snapshot(
        reaction_dir,
        inputs.selected_inp,
        selected_input_xyz=inputs.artifacts.selected_input_xyz,
        resource_request=inputs.prepared_input.resource_request,
        orca_executable=cfg.paths.orca_executable,
        queue_root=queue_root,
        snapshot_intent_token=intent_token,
        normalized_selected_payload=inputs.prepared_input.normalized_payload,
        source_selected_payload=inputs.source_payload,
    )


def _cleanup_submission_snapshot(reaction_dir: Path, execution_snapshot: Any) -> None:
    """Compensation: remove a generation the queue never took ownership of."""
    cleanup_unowned_orca_execution_snapshot(
        _snapshot_cleanup_job_dir(reaction_dir, execution_snapshot),
        execution_snapshot,
    )


def create_queued_submission(
    cfg: Any,
    args: Any,
    reaction_dir: Path,
    *,
    selected_inp: Path,
) -> QueuedSubmissionResult:
    """Submit one ORCA input directory to the durable queue.

    Stages, in order: prepare inputs, build the execution snapshot under a
    fresh intent, assemble the queue metadata, publish the row and job record,
    then take ownership of the snapshot. The parent worker owns queued delivery.
    A failure between snapshot creation and publication removes the unowned generation;
    a compensated publication failure removes it through the driver.
    """
    queue_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    inputs = _prepare_submission_inputs(cfg, args, selected_inp)
    intent_token = timestamped_token("snapshot_intent", token_bytes=16)
    execution_snapshot = _build_execution_snapshot(
        cfg, reaction_dir, inputs, queue_root=queue_root, intent_token=intent_token
    )
    try:
        queue_metadata = build_queue_metadata(
            reaction_dir=reaction_dir,
            artifacts=inputs.artifacts,
            job_type=inputs.job_type,
            molecule_key=inputs.molecule_key,
            resource_request=inputs.prepared_input.resource_request,
            execution_snapshot=execution_snapshot,
        )
        task_id = timestamped_token("orca", token_bytes=16)
        transition_snapshot_intent(
            queue_root,
            intent_token,
            target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
            expected_states={SNAPSHOT_INTENT_STATE_CREATING},
        )
    except BaseException:
        _cleanup_submission_snapshot(reaction_dir, execution_snapshot)
        raise
    outcome = run_enqueue_publication(
        cfg,
        reaction_dir,
        task_id=task_id,
        priority=inputs.priority,
        force=inputs.force,
        metadata=queue_metadata,
        on_compensated_failure=lambda: _cleanup_submission_snapshot(
            reaction_dir, execution_snapshot
        ),
    )
    entry = outcome.entry
    marker_warning = mark_snapshot_intent_owned(
        queue_root, intent_token, intent_label="queued ORCA snapshot"
    )
    if outcome.cancelled:
        raise QueuePublicationCancelledError(
            f"ORCA queue entry was cancelled before publication: {entry.queue_id}"
        )
    worker_pid = read_worker_pid_file(queue_root)
    worker_log = entry.metadata.get("worker_log")
    warnings = [warning for warning in (marker_warning, *outcome.warnings) if warning]
    return QueuedSubmissionResult(
        entry=entry,
        worker_info=WorkerStatusInfo(
            status="inactive" if worker_pid is None else "running",
            pid=worker_pid,
            log_file=worker_log if isinstance(worker_log, (str, Path)) and worker_log else None,
            detail="; ".join(warnings) or None,
        ),
    )


def submit_reaction_dir_to_queue(args: Any, *, cfg: AppConfig) -> DirectQueueSubmission:
    target = resolve_submission_target(args, cfg=cfg)
    if target is None:
        return DirectQueueSubmission(
            status="failed",
            reason="invalid_submission_target",
            stderr="failed to resolve ORCA submission target",
        )

    try:
        conflict_error = find_submission_conflict(target.allowed_root, target.reaction_dir)
    except QueueStoreCorruptError as exc:
        return DirectQueueSubmission(
            status="failed",
            reason="queue_store_corrupt",
            stderr=str(exc),
            target=target,
        )
    if conflict_error is not None:
        return DirectQueueSubmission(
            status="failed",
            reason="submission_conflict",
            stderr=conflict_error,
            target=target,
        )

    try:
        queued = create_queued_submission(
            target.cfg,
            args,
            target.reaction_dir,
            selected_inp=target.selected_inp,
        )
    except (DuplicateEntryError, DeadRunningRowUnjudgeableError) as exc:
        # Both are this directory's own queue row standing in the way: an
        # active duplicate, or a dead RUNNING row whose slot protection cannot
        # be read. The message carries the hint; no traceback.
        return DirectQueueSubmission(
            status="failed",
            reason="submission_conflict",
            stderr=str(exc),
            target=target,
        )
    except ValueError as exc:
        # e.g. a reaction dir without any .inp: fail cleanly instead of
        # leaking a traceback through the CLI.
        return DirectQueueSubmission(
            status="failed",
            reason="invalid_submission_input",
            stderr=str(exc),
            target=target,
        )
    except QueuePublicationCancelledError as exc:
        return DirectQueueSubmission(
            status="failed",
            reason="submission_cancelled",
            stderr=str(exc),
            target=target,
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
            target=target,
        )
    return DirectQueueSubmission(status="submitted", target=target, queued_result=queued)
