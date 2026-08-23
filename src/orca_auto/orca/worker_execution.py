from __future__ import annotations

import argparse
import copy
import logging
import subprocess
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.engine_scratch import (
    attach_scratch_provenance_mapping_to_exception,
    scratch_provenance_from_exception,
)
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.engines.worker_child import (
    WORKER_CHILD_MODULE,
    build_worker_child_command_for_engine,
)
from orca_auto.core.queue.child.execution import (
    find_queue_entry_by_id,
    install_shutdown_request_handlers,
)
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.engine.child import (
    WorkerChildRunSpec,
    run_engine_worker_child_job,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    transition_snapshot_intent,
)
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.lifecycle import entry_status_is_running
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
)
from orca_auto.core.utils.persistence import timestamped_token

from .attempt.reporting import build_final_result, last_out_path_from_state
from .config import load_config
from .execution import execute_orca_run, existing_completed_out, recover_crashed_state
from .execution_binding import (
    build_orca_execution_snapshot,
    cleanup_unowned_orca_execution_snapshot,
    orca_execution_provenance,
    orca_execution_snapshot_generation_dir,
    orca_execution_started_evidence,
    verify_orca_execution_snapshot,
    verify_orca_snapshot_executable,
)
from .orca_runner import OrcaRunner, WorkerShutdownInterrupt
from .queue.adapter import (
    get_cancel_requested,
    list_queue,
    queue_entry_app_name,
    queue_entry_force,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    requeue_running_entry,
    update_metadata,
)
from .resource_directives import prepare_submission_resource_request
from .runtime.run_lock import acquire_run_lock
from .state import finalize_state, load_state
from .statuses import AnalyzerStatus
from .submission import mark_orca_snapshot_owned

logger = logging.getLogger(__name__)

RECOVERY_REBIND_LIMIT = 3
RECOVERY_REBIND_COUNT_METADATA_KEY = "recovery_rebind_count"

BackgroundRunJobProcess = subprocess.Popen
WORKER_JOB_MODULE = WORKER_CHILD_MODULE


class WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: Any):
        super().__init__("worker_shutdown")
        self.context = context


@dataclass(frozen=True)
class OrcaWorkerExecutionContext:
    entry: Any
    config_path: str
    reaction_dir: str
    force: bool
    admission_token: str | None
    admission_app_name: str | None
    admission_task_id: str | None
    selected_inp: str
    source_selected_inp: str
    selected_input_xyz: str
    resource_request: dict[str, int]
    max_retries: int
    execution_snapshot: dict[str, Any]
    orca_executable: str


@dataclass(frozen=True)
class OrcaWorkerExecutionOutcome:
    exit_code: int
    reaction_dir: str
    entry: Any


def _orca_worker_outcome_exit_code(outcome: OrcaWorkerExecutionOutcome) -> int:
    return int(outcome.exit_code)


build_worker_child_command = build_worker_child_command_for_engine("orca")


_WORKER_CHILD_RUN_SPEC = WorkerChildRunSpec(
    shutdown_exception_type=WorkerShutdownRequested,
    entry_ready_fn=lambda entry: (
        entry_status_is_running(entry) and entry_matches_engine_identity(entry, "orca")
    ),
    outcome_exit_code_fn=_orca_worker_outcome_exit_code,
)


def _canonical_admission_app_name(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text == ORCA_AUTO_ORCA_APP_NAME:
        return ORCA_AUTO_ORCA_APP_NAME
    return text


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


def _queue_entry_by_id(queue_root: Path, queue_id: str) -> Any | None:
    return find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=lambda root: list_queue(Path(root)),
    )


def _build_execution_context(
    cfg: Any,
    entry: Any,
    *,
    worker_config_path: str,
    admission_token: str | None,
) -> OrcaWorkerExecutionContext:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    raw_reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser()
    reaction_dir = raw_reaction_dir.resolve()
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    if (
        raw_reaction_dir.is_symlink()
        or not reaction_dir.is_relative_to(allowed_root)
        or not reaction_dir.is_dir()
    ):
        raise ValueError("Queued ORCA reaction directory is outside the configured root")
    selected_inp = str(metadata.get("selected_inp") or "").strip()
    source_selected_inp = str(metadata.get("source_selected_inp") or "").strip()
    selected_input_xyz = str(metadata.get("selected_input_xyz") or "").strip()
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError(
            "Queued ORCA entry predates immutable execution snapshots; drain or resubmit it"
        )
    resource_request = metadata.get("resource_request")
    if (
        not isinstance(resource_request, dict)
        or set(resource_request) != {"max_cores", "max_memory_gb"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in resource_request.values()
        )
    ):
        raise ValueError("Queued ORCA entry has no resource request")
    max_retries = metadata.get("max_retries")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("Queued ORCA entry has an invalid retry budget")
    # A generation whose bound input already has a completed, analyzer-verified
    # output legitimately carries runtime files: allow them so the run's
    # completed-adoption path can claim the finished result instead of dying
    # on the pristine check (the crash landed after ORCA finished but before
    # the queue row turned terminal).
    selected_inp_path = Path(selected_inp) if selected_inp else None
    completed_adoption = bool(
        selected_inp_path is not None
        and selected_inp_path.is_file()
        and _completed_out_or_none(selected_inp_path) is not None
    )
    verified_selected, orca_executable = verify_orca_execution_snapshot(
        reaction_dir,
        snapshot,
        expected_selected_inp=selected_inp,
        expected_source_selected_inp=source_selected_inp,
        expected_selected_input_xyz=selected_input_xyz,
        expected_resource_request=resource_request,
        expected_max_retries=max_retries,
        allow_runtime_outputs=completed_adoption,
    )
    return OrcaWorkerExecutionContext(
        entry=entry,
        config_path=worker_config_path,
        reaction_dir=str(reaction_dir),
        force=queue_entry_force(entry),
        admission_token=admission_token,
        admission_app_name=queue_entry_app_name(entry) or None,
        admission_task_id=queue_entry_task_id(entry) or None,
        selected_inp=str(verified_selected),
        source_selected_inp=source_selected_inp,
        selected_input_xyz=selected_input_xyz,
        resource_request=dict(resource_request),
        max_retries=max_retries,
        execution_snapshot=snapshot,
        orca_executable=orca_executable,
    )


def _run_orca_job_for_entry(
    cfg: Any,
    context: OrcaWorkerExecutionContext,
    _queue_root: Path,
    _options: _engine_execution.EngineWorkerOptions,
) -> int:
    execution_provenance = orca_execution_provenance(context.execution_snapshot)

    def cancel_requested() -> bool:
        return _options.should_cancel is not None and _options.should_cancel()

    def stop_requested() -> bool:
        return cancel_requested() or (
            _options.shutdown_requested is not None and _options.shutdown_requested()
        )

    class ShutdownAwareOrcaRunner(OrcaRunner):
        def __init__(self, _configured_orca_executable: str) -> None:
            super().__init__(context.orca_executable)
            self._runtime_outputs_started = False
            self.set_executable_identity(
                context.execution_snapshot["executable_identities"]["orca"]
            )
            self.set_durable_directory_identity(
                context.execution_snapshot["execution_dir_identity"]
            )
            self.set_shutdown_requested(stop_requested)

        def run(self, inp_path: Path) -> Any:
            current_input = require_confined_regular_file(
                Path(context.execution_snapshot["execution_dir"]),
                inp_path,
                label="ORCA queued execution input",
            )
            if (
                current_input.parent != Path(context.execution_snapshot["execution_dir"]).resolve()
                or current_input.suffix.lower() != ".inp"
            ):
                raise ValueError("ORCA queued execution input must be a private .inp file")
            verify_orca_execution_snapshot(
                context.reaction_dir,
                context.execution_snapshot,
                expected_selected_inp=context.selected_inp,
                expected_source_selected_inp=context.source_selected_inp,
                expected_selected_input_xyz=context.selected_input_xyz,
                expected_resource_request=context.resource_request,
                expected_max_retries=context.max_retries,
                allow_runtime_outputs=self._runtime_outputs_started,
            )
            try:
                result = super().run(inp_path)
            except BaseException as run_exc:
                self._runtime_outputs_started = True
                try:
                    verify_orca_execution_snapshot(
                        context.reaction_dir,
                        context.execution_snapshot,
                        expected_selected_inp=context.selected_inp,
                        expected_source_selected_inp=context.source_selected_inp,
                        expected_selected_input_xyz=context.selected_input_xyz,
                        expected_resource_request=context.resource_request,
                        expected_max_retries=context.max_retries,
                        allow_runtime_outputs=True,
                    )
                except BaseException as verify_exc:
                    provenance = scratch_provenance_from_exception(run_exc)
                    if provenance:
                        attach_scratch_provenance_mapping_to_exception(verify_exc, provenance)
                    raise
                raise
            self._runtime_outputs_started = True
            try:
                verify_orca_execution_snapshot(
                    context.reaction_dir,
                    context.execution_snapshot,
                    expected_selected_inp=context.selected_inp,
                    expected_source_selected_inp=context.source_selected_inp,
                    expected_selected_input_xyz=context.selected_input_xyz,
                    expected_resource_request=context.resource_request,
                    expected_max_retries=context.max_retries,
                    allow_runtime_outputs=True,
                )
            except BaseException as verify_exc:
                result_provenance = getattr(result, "scratch_provenance", None)
                if isinstance(result_provenance, dict) and result_provenance:
                    attach_scratch_provenance_mapping_to_exception(
                        verify_exc,
                        result_provenance,
                    )
                raise
            result.execution_provenance = dict(execution_provenance)
            return result

    bound_cfg = copy.copy(cfg)
    bound_cfg.runtime = copy.copy(cfg.runtime)
    bound_cfg.runtime.default_max_retries = context.max_retries
    bound_cfg.resources = replace(
        cfg.resources,
        max_cores_per_task=context.resource_request["max_cores"],
        max_memory_gb_per_task=context.resource_request["max_memory_gb"],
    )

    try:
        return execute_run_job(
            context.config_path,
            context.reaction_dir,
            selected_inp=context.selected_inp,
            cfg=bound_cfg,
            force=context.force,
            reservation_token=context.admission_token,
            admission_app_name=context.admission_app_name,
            admission_task_id=context.admission_task_id,
            execution_provenance=execution_provenance,
            queue_id=queue_entry_id(context.entry),
            queue_generation=queue_entry_generation_token(context.entry),
            runner_cls=ShutdownAwareOrcaRunner,
        )
    except WorkerShutdownInterrupt as exc:
        if cancel_requested():
            state = load_state(Path(context.reaction_dir))
            if state is not None and not isinstance(state.get("final_result"), dict):
                cancelled_result = build_final_result(
                    status="cancelled",
                    analyzer_status=AnalyzerStatus.INCOMPLETE,
                    reason="cancel_requested",
                    last_out_path=last_out_path_from_state(state),
                )
                finalize_state(
                    Path(context.reaction_dir),
                    state,
                    status="cancelled",
                    final_result=cancelled_result,
                )
        raise WorkerShutdownRequested(context) from exc


def _maybe_rebind_recovery_generation(
    entry: Any,
    *,
    queue_root: Path,
    cfg_factory: Callable[[], Any],
) -> Any:
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
            # generation: the ordinary claim path adopts the completed output
            # instead of re-running the whole calculation in a rebind.
            return entry
    if get_cancel_requested(queue_root, str(entry.queue_id), expected_entry=entry):
        # Honor the pending cancellation through the requeue chokepoint (which
        # turns a cancel-requested running row terminal) instead of letting the
        # strict claim-time verification misreport the crashed generation as a
        # corrupt failure.
        requeue_running_entry(
            queue_root,
            str(entry.queue_id),
            expected_entry=entry,
        )
        refreshed = _queue_entry_by_id(queue_root, str(entry.queue_id))
        return refreshed if refreshed is not None else entry
    raw_count = metadata.get(RECOVERY_REBIND_COUNT_METADATA_KEY, 0)
    count = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
        else RECOVERY_REBIND_LIMIT
    )
    if count >= RECOVERY_REBIND_LIMIT:
        raise ValueError(
            "ORCA crash recovery limit reached for this submission "
            f"({count}/{RECOVERY_REBIND_LIMIT}); resubmit the job to continue"
        )
    cfg = cfg_factory()
    recovery_executable = verify_orca_snapshot_executable(
        snapshot,
        expected_executable=cfg.paths.orca_executable,
    )
    if not update_metadata(
        queue_root,
        str(entry.queue_id),
        {RECOVERY_REBIND_COUNT_METADATA_KEY: count + 1},
        expected_entry=entry,
    ):
        raise ValueError("ORCA crash recovery could not reserve its rebind budget on the queue row")
    claimed = _queue_entry_by_id(queue_root, str(entry.queue_id))
    claimed_metadata = getattr(claimed, "metadata", None)
    if (
        claimed is None
        or not isinstance(claimed_metadata, dict)
        or claimed_metadata.get(RECOVERY_REBIND_COUNT_METADATA_KEY) != count + 1
    ):
        raise ValueError("ORCA crash recovery lost its rebind budget reservation")

    source_selected = str(metadata.get("source_selected_inp") or "").strip()
    if not source_selected:
        raise ValueError("ORCA crash recovery requires the submission source input path")
    max_retries = metadata.get("max_retries")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("ORCA crash recovery found an invalid retry budget on the queue row")
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
            max_retries=max_retries,
            orca_executable=recovery_executable,
            queue_root=queue_root,
            snapshot_intent_token=timestamped_token("snapshot_intent", token_bytes=16),
            normalized_selected_payload=prepared.normalized_payload,
            source_selected_sha256=prepared.source_sha256,
            recovery_from=snapshot,
        )
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
            },
            expected_entry=claimed,
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
    if not isinstance(updated_snapshot, dict) or str(
        updated_snapshot.get("generation_name") or ""
    ) != str(new_snapshot.get("generation_name") or ""):
        raise ValueError("ORCA crash recovery lost its replacement queue row")
    logger.warning(
        "Recovered crashed ORCA job %s into replacement generation %s (rebind %d/%d)%s",
        str(entry.queue_id),
        str(new_snapshot.get("generation_name") or ""),
        count + 1,
        RECOVERY_REBIND_LIMIT,
        f"; {marker_warning}" if marker_warning else "",
    )
    return updated


def _recovering_queue_entry_by_id(config_path: str) -> Callable[[Path, str], Any | None]:
    def find(queue_root: Path, queue_id: str) -> Any | None:
        entry = _queue_entry_by_id(queue_root, queue_id)
        if entry is None or not entry_matches_engine_identity(entry, "orca"):
            return entry
        return _maybe_rebind_recovery_generation(
            entry,
            queue_root=Path(queue_root),
            cfg_factory=lambda: load_config(config_path),
        )

    return find


def _worker_execution_spec(
    *,
    worker_config_path: str,
    admission_token: str | None,
) -> _engine_execution.EngineWorkerExecutionSpec:
    return _engine_execution.build_engine_worker_execution_spec(
        build_context=lambda cfg_obj, entry_obj: _build_execution_context(
            cfg_obj,
            entry_obj,
            worker_config_path=worker_config_path,
            admission_token=admission_token,
        ),
        shutdown_exception_type=WorkerShutdownRequested,
        mark_running=lambda _cfg, _context, _options: None,
        run_job=_run_orca_job_for_entry,
        finalize_entry=lambda _cfg, _context, result, _queue_root, _options: result,
        build_outcome=lambda context, result, _finalized: OrcaWorkerExecutionOutcome(
            exit_code=int(result),
            reaction_dir=context.reaction_dir,
            entry=context.entry,
        ),
    )


def process_dequeued_entry(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None = None,
    worker_config_path: str,
    admission_token: str | None = None,
    dependencies: Any | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
) -> OrcaWorkerExecutionOutcome:
    # execute_locked_run rebuilds the same durable registrar from the resolved
    # admission root/token so every retry attempt is fenced inside OrcaRunner.
    del dependencies, prepare_running_job, register_running_job
    if queue_root is None:
        raise ValueError("queue_root is required for ORCA worker execution")
    return _engine_execution.run_engine_worker_entry_with_spec_factory_options(
        cfg,
        entry,
        queue_root=queue_root,
        spec_factory=lambda: _worker_execution_spec(
            worker_config_path=worker_config_path,
            admission_token=admission_token,
        ),
        shutdown_requested=shutdown_requested,
        should_cancel=lambda: get_cancel_requested(
            queue_root,
            str(entry.queue_id),
            expected_entry=entry,
        ),
    )


def _mark_recovery_pending_context(_cfg: Any, _context: Any, *, reason: str) -> None:
    del reason


def run_worker_child_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    await_parent_admission_handoff_fn: Callable[[Any, str], bool] | None = None,
) -> int:
    worker_child_kwargs: dict[str, Any] = {
        "spec": _WORKER_CHILD_RUN_SPEC,
        "config_path": config_path,
        "queue_root": queue_root,
        "queue_id": queue_id,
        "admission_token": admission_token,
        "load_config_fn": load_config,
        "find_queue_entry_fn": _recovering_queue_entry_by_id(config_path),
        "admission_root_fn": resolve_admission_root,
        "install_signal_handlers_fn": lambda controller: install_shutdown_request_handlers(
            controller,
            install_signal_handlers_fn=install_shutdown_signal_handlers,
        ),
        "process_dequeued_entry_fn": process_dequeued_entry,
        "dependencies_fn": lambda: None,
        "requeue_running_entry_fn": requeue_running_entry,
        "mark_recovery_pending_context_fn": _mark_recovery_pending_context,
        "process_dequeued_entry_kwargs": {
            "worker_config_path": config_path,
            "admission_token": admission_token,
        },
    }
    if await_parent_admission_handoff_fn is not None:
        worker_child_kwargs["await_parent_admission_handoff_fn"] = await_parent_admission_handoff_fn
    return run_engine_worker_child_job(**worker_child_kwargs)


def execute_run_job(
    config_path: str,
    reaction_dir: str,
    *,
    selected_inp: str | Path | None = None,
    cfg: Any | None = None,
    force: bool = False,
    reservation_token: str | None = None,
    admission_app_name: str | None = None,
    admission_task_id: str | None = None,
    execution_provenance: dict[str, Any] | None = None,
    queue_id: str | None = None,
    queue_generation: str | None = None,
    runner_cls: type[OrcaRunner] = OrcaRunner,
) -> int:
    return execute_orca_run(
        Namespace(
            config=config_path,
            reaction_dir=reaction_dir,
            force=force,
        ),
        runner_cls=runner_cls,
        cfg=cfg,
        reaction_dir=Path(reaction_dir).expanduser().resolve(),
        selected_inp=(Path(selected_inp).expanduser().resolve() if selected_inp else None),
        reservation_token=reservation_token,
        admission_app_name=_canonical_admission_app_name(admission_app_name),
        admission_task_id=admission_task_id,
        execution_provenance=execution_provenance,
        queue_id=queue_id,
        queue_generation=queue_generation,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {WORKER_JOB_MODULE}")
    parser.add_argument("--config", required=True)
    parser.add_argument("--queue-root", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--admission-token", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_worker_child_job(
        config_path=args.config,
        queue_root=str(args.queue_root).strip(),
        queue_id=str(args.queue_id).strip(),
        admission_token=str(args.admission_token).strip() or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BackgroundRunJobProcess",
    "OrcaWorkerExecutionContext",
    "OrcaWorkerExecutionOutcome",
    "WORKER_JOB_MODULE",
    "WorkerShutdownRequested",
    "build_parser",
    "build_worker_child_command",
    "execute_run_job",
    "main",
    "process_dequeued_entry",
    "run_worker_child_job",
]
