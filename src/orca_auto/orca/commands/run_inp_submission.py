from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.queue.types import QueueEntry, QueueStatus

from ..input_artifacts import selected_input_artifacts
from ..retry_policy import effective_max_retries

if TYPE_CHECKING:
    from ..types import QueueEnqueuedNotification
    from .run_inp_context import WorkerStatusInfo

logger = logging.getLogger(__name__)


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


def active_queue_entry(allowed_root: Path, reaction_dir: Path, *, deps: Any) -> QueueEntry | None:
    queue_adapter = deps.submission.queue_adapter
    helper = getattr(queue_adapter, "get_active_entry_for_reaction_dir", None)
    if callable(helper):
        return helper(allowed_root, str(reaction_dir))

    resolved = str(reaction_dir.expanduser().resolve())
    for entry in queue_adapter.list_queue(allowed_root):
        if queue_adapter.queue_entry_reaction_dir(entry) != resolved:
            continue
        if queue_adapter.queue_entry_status(entry) in {
            QueueStatus.PENDING.value,
            QueueStatus.RUNNING.value,
        }:
            return entry
    return None


def find_submission_conflict(
    allowed_root: Path,
    reaction_dir: Path,
    *,
    deps: Any,
) -> str | None:
    active_entry = active_queue_entry(allowed_root, reaction_dir, deps=deps)
    queue_adapter = deps.submission.queue_adapter
    if active_entry is not None:
        return (
            "Job directory already queued: "
            f"{reaction_dir} (queue_id={queue_adapter.queue_entry_id(active_entry)}, "
            f"status={queue_adapter.queue_entry_status(active_entry)})"
        )
    return deps.submission.active_direct_run_error(reaction_dir)


def emit_queued_submission(
    reaction_dir: Path,
    entry: QueueEntry,
    *,
    worker_status: str | None,
    worker_pid: int | None,
    worker_log: str | Path | None,
    worker_detail: str | None = None,
    deps: Any,
) -> None:
    queue_adapter = deps.submission.queue_adapter
    print("status: queued")
    print(f"job_dir: {reaction_dir}")
    print(f"queue_id: {queue_adapter.queue_entry_id(entry)}")
    task_id = queue_adapter.queue_entry_task_id(entry)
    if task_id:
        print(f"job_id: {task_id}")
    print(f"priority: {queue_adapter.queue_entry_priority(entry)}")
    if queue_adapter.queue_entry_force(entry):
        print("force: true")
    if worker_status:
        print(f"worker: {worker_status}")
    if worker_pid is not None:
        print(f"worker_pid: {worker_pid}")
    if worker_log:
        print(f"worker_log: {worker_log}")
    if worker_detail:
        print(f"worker_detail: {worker_detail}")


def worker_status_for_submission(allowed_root: Path) -> WorkerStatusInfo:
    from ..queue.worker import read_worker_pid
    from .run_inp_context import WorkerStatusInfo

    pid = read_worker_pid(allowed_root)
    if pid is None:
        return WorkerStatusInfo(status="inactive")
    return WorkerStatusInfo(status="running", pid=pid)


def queue_entry_worker_log(entry: Any, *, deps: Any) -> Any | None:
    queue_adapter = deps.submission.queue_adapter
    metadata_fn = getattr(queue_adapter, "queue_entry_metadata", None)
    if not callable(metadata_fn):
        return None
    metadata = metadata_fn(entry)
    if not isinstance(metadata, dict):
        return None
    worker_log = metadata.get("worker_log")
    if isinstance(worker_log, (str, Path)):
        return worker_log
    return None


def worker_status_with_log_file(
    worker_info: WorkerStatusInfo,
    worker_log: Any | None,
) -> WorkerStatusInfo:
    from .run_inp_context import WorkerStatusInfo

    return WorkerStatusInfo(
        status=worker_info.status,
        pid=worker_info.pid,
        log_file=worker_log or worker_info.log_file,
        detail=worker_info.detail,
    )


def build_queue_enqueued_notification(entry: Any, *, deps: Any) -> QueueEnqueuedNotification:
    submission = deps.submission
    return {
        "queue_id": submission.queue_adapter.queue_entry_id(entry),
        "reaction_dir": submission.queue_adapter.queue_entry_reaction_dir(entry),
        "priority": submission.queue_adapter.queue_entry_priority(entry),
        "force": submission.queue_adapter.queue_entry_force(entry),
        "enqueued_at": getattr(entry, "enqueued_at", ""),
    }


def resource_request_from_selected_inp(
    cfg: Any,
    selected_inp: Path | None,
    *,
    deps: Any,
    logger: logging.Logger,
) -> dict[str, int]:
    if selected_inp is None:
        raise ValueError("No .inp file selected for ORCA queue submission.")
    resource_request, actions = deps.submission.ensure_submission_resource_request(
        selected_inp,
        default_max_cores=int(cfg.resources.max_cores_per_task),
        default_max_memory_gb=int(cfg.resources.max_memory_gb_per_task),
    )
    if actions:
        logger.info(
            "Updated ORCA input resource directives in %s: %s",
            selected_inp,
            ", ".join(actions),
        )
    return resource_request


def warn_ignored_resource_override_flags(args: Any, *, logger: logging.Logger) -> None:
    if getattr(args, "max_cores", None) is None and getattr(args, "max_memory_gb", None) is None:
        return
    logger.warning(
        "Standalone ORCA queue submission ignores --max-cores/--max-memory-gb; "
        "resource metadata is read from the input file."
    )


def build_queue_metadata(
    cfg: Any,
    *,
    reaction_dir: Path,
    selected_inp: Path | None,
    args: Any | None = None,
    deps: Any,
) -> dict[str, Any]:
    del args
    from ..job_locations import resolve_job_metadata

    artifacts = selected_input_artifacts(selected_inp)
    job_type, molecule_key = resolve_job_metadata(artifacts.selected_inp, reaction_dir)
    requested = resource_request_from_selected_inp(cfg, selected_inp, deps=deps, logger=logger)
    assert selected_inp is not None
    metadata: dict[str, Any] = {
        "submitted_via": "run_inp",
        "max_retries": effective_max_retries(
            selected_inp,
            configured_max_retries=max(0, int(cfg.runtime.default_max_retries)),
        ),
        "job_type": job_type,
        "molecule_key": molecule_key,
        "resource_request": requested,
        "resource_actual": dict(requested),
    }
    if artifacts.selected_inp:
        metadata["selected_inp"] = artifacts.selected_inp
        metadata["selected_input_path"] = artifacts.selected_input_path
    metadata["selected_input_xyz"] = artifacts.selected_input_xyz
    return metadata


def upsert_queued_job_record(
    cfg: Any,
    *,
    reaction_dir: Path,
    selected_inp: Path | None,
    job_id: str,
    queue_metadata: dict[str, Any] | None = None,
    deps: Any,
) -> None:
    from ..job_locations import resolve_job_metadata, upsert_job_record

    artifacts = selected_input_artifacts(selected_inp)
    selected_input = artifacts.selected_input_path
    metadata = dict(queue_metadata or {})
    job_type = str(metadata.get("job_type") or "").strip()
    molecule_key = str(metadata.get("molecule_key") or "").strip()
    if not job_type or not molecule_key:
        derived_job_type, derived_molecule_key = resolve_job_metadata(
            artifacts.selected_inp or selected_input,
            reaction_dir,
        )
        job_type = job_type or derived_job_type
        molecule_key = molecule_key or derived_molecule_key
    requested = metadata.get("resource_request")
    if not isinstance(requested, dict):
        requested = {}
    if not requested and selected_inp is not None and selected_inp.exists():
        requested = deps.submission.read_resource_request_from_input(selected_inp)
    if not requested and selected_inp is not None and selected_inp.exists():
        requested = resource_request_from_selected_inp(cfg, selected_inp, deps=deps, logger=logger)
    actual = metadata.get("resource_actual")
    if not isinstance(actual, dict):
        actual = dict(requested)
    upsert_job_record(
        cfg,
        job_id=job_id,
        status="queued",
        job_dir=reaction_dir,
        job_type=job_type,
        selected_input_xyz=selected_input,
        molecule_key=molecule_key,
        resource_request=requested,
        resource_actual=actual,
    )


def record_queued_job_side_effect(
    cfg: Any,
    *,
    reaction_dir: Path,
    selected_inp: Path | None,
    job_id: str,
    queue_metadata: dict[str, Any],
    deps: Any,
) -> str | None:
    try:
        upsert_queued_job_record(
            cfg,
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            job_id=job_id,
            queue_metadata=queue_metadata,
            deps=deps,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "queued job record update failed after queue submission succeeded: "
            "reaction_dir=%s job_id=%s error=%s",
            reaction_dir,
            job_id,
            exc,
            exc_info=True,
        )
        return "queued job record update failed; queue submission succeeded"
    return None


def worker_status_with_detail(
    worker_info: WorkerStatusInfo,
    detail: str | None,
) -> WorkerStatusInfo:
    from .run_inp_context import WorkerStatusInfo

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


def create_queued_submission(
    cfg: Any,
    args: Any,
    reaction_dir: Path,
    *,
    selected_inp: Path | None = None,
    deps: Any,
) -> QueuedSubmissionResult:
    from ..queue.adapter import enqueue

    submission = deps.submission
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    if selected_inp is None:
        try:
            selected_inp = submission.select_latest_inp(reaction_dir)
        except ValueError:
            selected_inp = None
    warn_ignored_resource_override_flags(args, logger=logger)
    queue_metadata = build_queue_metadata(
        cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        args=args,
        deps=deps,
    )
    entry = enqueue(
        allowed_root,
        str(reaction_dir),
        priority=int(getattr(args, "priority", 10)),
        force=bool(getattr(args, "force", False)),
        metadata=queue_metadata,
    )

    task_id = submission.queue_adapter.queue_entry_task_id(entry)
    side_effect_warning = None
    if task_id:
        side_effect_warning = record_queued_job_side_effect(
            cfg,
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            job_id=str(task_id),
            queue_metadata=queue_metadata,
            deps=deps,
        )

    worker_info = worker_status_with_log_file(
        worker_status_for_submission(allowed_root),
        queue_entry_worker_log(entry, deps=deps),
    )
    worker_info = worker_status_with_detail(worker_info, side_effect_warning)
    return QueuedSubmissionResult(
        entry=entry,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        queue_metadata=queue_metadata,
        worker_info=worker_info,
    )


def notify_queued_submission(
    cfg: Any,
    result: QueuedSubmissionResult,
    *,
    deps: Any,
) -> None:
    notification = build_queue_enqueued_notification(result.entry, deps=deps)
    deps.notifications.notify_queue_enqueued_event(cfg.telegram, notification)


def submit_reaction_dir_to_queue(
    args: Any,
    *,
    deps: Any,
) -> DirectQueueSubmission:
    context = deps.submission.resolve_submission_context(args)
    if context is None:
        return DirectQueueSubmission(
            status="failed",
            reason="invalid_submission_target",
            stderr="failed to resolve ORCA submission target",
        )

    conflict_error = find_submission_conflict(
        context.allowed_root,
        context.reaction_dir,
        deps=deps,
    )
    if conflict_error is not None:
        return DirectQueueSubmission(
            status="failed",
            reason="submission_conflict",
            stderr=conflict_error,
            context=context,
        )

    try:
        from ..queue.adapter import DuplicateEntryError

        queued = create_queued_submission(
            context.cfg,
            args,
            context.reaction_dir,
            selected_inp=context.selected_inp,
            deps=deps,
        )
        notify_queued_submission(context.cfg, queued, deps=deps)
    except DuplicateEntryError as exc:
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
    return DirectQueueSubmission(status="submitted", context=context, queued_result=queued)


def cmd_run_inp_submit(
    args: Any,
    *,
    runner_cls: type[Any],
    deps: Any,
    logger: logging.Logger,
) -> int:
    del runner_cls
    submission = deps.submission.submit_reaction_dir_to_queue(args)
    if submission.status != "submitted":
        if submission.stderr:
            logger.error("%s", submission.stderr.rstrip())
        return 1

    result = submission.queued_result
    context = submission.context
    if result is None or context is None:
        logger.error("ORCA queue submission did not return a queued result.")
        return 1
    worker_info = result.worker_info
    deps.submission.emit_queued_submission(
        context.reaction_dir,
        result.entry,
        worker_status=worker_info.status,
        worker_pid=worker_info.pid,
        worker_log=worker_info.log_file,
        worker_detail=worker_info.detail,
    )
    return 0
