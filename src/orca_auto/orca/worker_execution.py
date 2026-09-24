"""Worker-child execution of one claimed ORCA queue row.

``run_worker_child_job`` is the child process entry point: it looks up the
claimed row, lets ``recovery_rebind`` move a crash-interrupted
claim into a replacement generation, waits for the parent's admission
hand-off, and then runs the bound generation through ``execute_orca_run``
under a shutdown-aware runner that re-verifies the immutable snapshot around
the engine launch. Pre-launch rejections are recorded on the queue row,
RAM-scratch capacity refusals return the row to the queue (exit
``ADMISSION_DEFERRED_EXIT_CODE``), and a shutdown or cancel during the run
finalizes ``job_state.json`` before the row is requeued.
"""

from __future__ import annotations

import copy
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.confined_io import require_confined_regular_file
from orca_auto.core.engine_scratch import (
    EngineScratchCapacityError,
    attach_scratch_provenance_mapping_to_exception,
    scratch_provenance_from_exception,
)
from orca_auto.core.queue.child.execution import (
    ChildWorkerShutdownController,
    find_queue_entry_by_id,
)
from orca_auto.core.queue.child.process import entry_status_is_running
from orca_auto.core.queue.engine.child import await_parent_admission_handoff
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
)
from orca_auto.orca.queue.identity import entry_matches_engine_identity

from .admission_env import (
    ADMISSION_APP_NAME_ENV_VAR,
    ADMISSION_TASK_ID_ENV_VAR,
    ADMISSION_TOKEN_ENV_VAR,
)
from .attempt.reporting import build_final_result, last_out_path_from_state
from .config import AppConfig, load_config
from .execution import execute_orca_run
from .execution_binding import (
    orca_execution_provenance,
    verify_orca_execution_snapshot,
)
from .orca_runner import OrcaRunner, RunResult, WorkerShutdownInterrupt
from .output_adoption import completed_out_or_none
from .queue.adapter import (
    cancellation_probe,
    list_queue,
    mark_failed,
    queue_entry_app_name,
    queue_entry_force,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    requeue_running_entry,
)
from .queue.entries import queue_entry_is_retired_workflow_owned

# The rebind keeps its private worker-facing name: the child looks it up
# through this module, which is also where tests substitute it. Its metadata
# keys stay reachable here for the row inspections that predate the split.
from .recovery_rebind import (
    RECOVERY_REBIND_CLAIM_METADATA_KEY as RECOVERY_REBIND_CLAIM_METADATA_KEY,
)
from .recovery_rebind import (
    RECOVERY_REBIND_COUNT_METADATA_KEY as RECOVERY_REBIND_COUNT_METADATA_KEY,
)
from .recovery_rebind import RECOVERY_REBIND_LIMIT as RECOVERY_REBIND_LIMIT
from .recovery_rebind import (
    maybe_rebind_recovery_generation as _maybe_rebind_recovery_generation,
)
from .run_context import RunExecutionContext, configured_admission_root
from .run_lock import acquire_run_lock
from .state import finalize_state
from .state_reading import load_state
from .statuses import AnalyzerStatus, RunStatus

logger = logging.getLogger(__name__)

# EX_TEMPFAIL: the child returned its row to the queue before ORCA started.
ADMISSION_DEFERRED_EXIT_CODE = 75
BackgroundRunJobProcess = subprocess.Popen
WORKER_JOB_MODULE = "orca_auto.orca.commands.worker_child"


@dataclass(frozen=True)
class OrcaWorkerExecutionContext:
    entry: QueueEntry
    reaction_dir: str
    force: bool
    admission_token: str | None
    admission_app_name: str | None
    admission_task_id: str | None
    selected_inp: str
    source_selected_inp: str
    selected_input_xyz: str
    resource_request: dict[str, int]
    execution_snapshot: dict[str, Any]
    orca_executable: str

    def verify_snapshot(self, *, allow_runtime_outputs: bool) -> tuple[Path, str]:
        return verify_orca_execution_snapshot(
            self.reaction_dir,
            self.execution_snapshot,
            expected_selected_inp=self.selected_inp,
            expected_source_selected_inp=self.source_selected_inp,
            expected_selected_input_xyz=self.selected_input_xyz,
            expected_resource_request=self.resource_request,
            allow_runtime_outputs=allow_runtime_outputs,
        )


@dataclass(frozen=True)
class OrcaWorkerExecutionOutcome:
    exit_code: int
    reaction_dir: str
    entry: QueueEntry


def build_worker_child_command(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
) -> list[str]:
    """Argv for the child job process (``python -m orca_auto.orca.commands.worker_child``)."""
    command = [
        sys.executable,
        "-m",
        WORKER_JOB_MODULE,
        "--config",
        str(config_path),
        "--queue-root",
        str(queue_root),
        "--queue-id",
        str(queue_id),
    ]
    if admission_token:
        command.extend(["--admission-token", str(admission_token)])
    return command


class WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: OrcaWorkerExecutionContext) -> None:
        super().__init__("worker_shutdown")
        self.context = context


def _explicit_or_env(value: str | None, env_var: str) -> str | None:
    if value is not None:
        return value
    return (os.getenv(env_var, "") or "").strip() or None


def _queue_entry_by_id(queue_root: Path, queue_id: str) -> QueueEntry | None:
    return find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue,
    )


def _build_execution_context(
    cfg: AppConfig,
    entry: QueueEntry,
    *,
    admission_token: str | None,
) -> OrcaWorkerExecutionContext:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    if "max_retries" in metadata:
        raise ValueError("Queued ORCA entry contains a removed execution setting; resubmit the job")
    raw_reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser()
    reaction_dir = raw_reaction_dir.resolve()
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    if (
        raw_reaction_dir.is_symlink()
        or not reaction_dir.is_relative_to(allowed_root)
        or not reaction_dir.is_dir()
    ):
        raise ValueError("Queued ORCA reaction directory is outside the configured root")
    if queue_entry_is_retired_workflow_owned(entry, allowed_root):
        raise ValueError(
            "Queued ORCA directory belongs to a retired workflow; use the previous runtime to drain or cancel it"
        )
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
    # A generation whose bound input already has a completed, analyzer-verified
    # output legitimately carries runtime files: allow them so the run can
    # settle the finished generation in place instead of dying on the pristine
    # check (the crash landed after ORCA finished but before the queue row
    # turned terminal). The run settles from a recorded attempt verdict when
    # one exists, which may be a failure, and adopts the output otherwise.
    selected_inp_path = Path(selected_inp) if selected_inp else None
    completed_adoption = bool(
        selected_inp_path is not None
        and selected_inp_path.is_file()
        and completed_out_or_none(selected_inp_path) is not None
    )
    verified_selected, orca_executable = verify_orca_execution_snapshot(
        reaction_dir,
        snapshot,
        expected_selected_inp=selected_inp,
        expected_source_selected_inp=source_selected_inp,
        expected_selected_input_xyz=selected_input_xyz,
        expected_resource_request=resource_request,
        allow_runtime_outputs=completed_adoption,
    )
    return OrcaWorkerExecutionContext(
        entry=entry,
        reaction_dir=str(reaction_dir),
        force=queue_entry_force(entry),
        admission_token=admission_token,
        admission_app_name=queue_entry_app_name(entry) or None,
        admission_task_id=queue_entry_task_id(entry) or None,
        selected_inp=str(verified_selected),
        source_selected_inp=source_selected_inp,
        selected_input_xyz=selected_input_xyz,
        resource_request=dict(resource_request),
        execution_snapshot=snapshot,
        orca_executable=orca_executable,
    )


def _run_orca_job_for_entry(
    cfg: AppConfig,
    context: OrcaWorkerExecutionContext,
    queue_root: Path,
    *,
    should_cancel: Callable[[], bool],
    shutdown_requested: Callable[[], bool] | None,
) -> int:
    if shutdown_requested is not None and shutdown_requested():
        raise WorkerShutdownRequested(context)
    execution_provenance = orca_execution_provenance(context.execution_snapshot)

    def stop_requested() -> bool:
        return should_cancel() or (shutdown_requested is not None and shutdown_requested())

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

        def prepare(self, inp_path: Path) -> None:
            # Staging reads the generation, so keep run()'s verify-before-stage
            # order. A snapshot that fails here is left for run() to report.
            try:
                context.verify_snapshot(allow_runtime_outputs=False)
            except Exception:  # noqa: BLE001
                return
            super().prepare(inp_path)

        def run(self, inp_path: Path) -> RunResult:
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
            context.verify_snapshot(allow_runtime_outputs=self._runtime_outputs_started)
            try:
                result = super().run(inp_path)
            except BaseException as run_exc:
                self._runtime_outputs_started = True
                try:
                    context.verify_snapshot(allow_runtime_outputs=True)
                except BaseException as verify_exc:
                    provenance = scratch_provenance_from_exception(run_exc)
                    if provenance:
                        attach_scratch_provenance_mapping_to_exception(verify_exc, provenance)
                    raise
                raise
            self._runtime_outputs_started = True
            try:
                context.verify_snapshot(allow_runtime_outputs=True)
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
    bound_cfg.resources = replace(
        cfg.resources,
        max_cores_per_task=context.resource_request["max_cores"],
        max_memory_gb_per_task=context.resource_request["max_memory_gb"],
    )

    try:
        execution = RunExecutionContext(
            cfg=bound_cfg,
            reaction_dir=Path(context.reaction_dir).expanduser().resolve(),
            selected_inp=Path(context.selected_inp).expanduser().resolve(),
            admission_root=configured_admission_root(bound_cfg),
            force=context.force,
            reservation_token=_explicit_or_env(context.admission_token, ADMISSION_TOKEN_ENV_VAR),
            admission_app_name=_explicit_or_env(
                context.admission_app_name, ADMISSION_APP_NAME_ENV_VAR
            ),
            admission_task_id=_explicit_or_env(
                context.admission_task_id, ADMISSION_TASK_ID_ENV_VAR
            ),
            execution_provenance=dict(execution_provenance),
            queue_id=queue_entry_id(context.entry) or None,
            queue_generation=queue_entry_generation_token(context.entry) or None,
        )
        return execute_orca_run(execution, runner_cls=ShutdownAwareOrcaRunner)
    except EngineScratchCapacityError as exc:
        return _defer_admission(context, queue_root, reason=str(exc))
    except WorkerShutdownInterrupt as exc:
        if should_cancel():
            _finalize_cancelled_run_state(Path(context.reaction_dir))
        raise WorkerShutdownRequested(context) from exc


def _finalize_cancelled_run_state(reaction_dir: Path) -> None:
    """Record the cancelled outcome the interrupted run never wrote.

    ``execute_orca_run`` released ``run.lock`` when the interrupt propagated,
    so this load -> finalize takes the same non-blocking lock every other
    ``job_state.json`` finalizer takes (``replay._record_terminal_run_state``,
    ``worker_tracking``) and cannot interleave with them.  When the lock is
    already held, fail closed by skipping: the shutdown path still marks the
    queue row cancelled with its replay marker, and the parent's terminal
    replay then calls ``record_cancelled_run_state`` under this lock, which
    writes the cancelled result or keeps a terminal one written meanwhile.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(acquire_run_lock(reaction_dir))
        except RuntimeError as exc:
            logger.warning(
                "Skipping cancel finalization of %s; run lock is held and terminal "
                "replay settles the state: %s",
                reaction_dir,
                exc,
            )
            return
        state = load_state(reaction_dir)
        if state is None or isinstance(state.get("final_result"), dict):
            return
        cancelled_result = build_final_result(
            status=RunStatus.CANCELLED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="cancel_requested",
            last_out_path=last_out_path_from_state(state),
        )
        finalize_state(
            reaction_dir,
            state,
            status=RunStatus.CANCELLED,
            final_result=cancelled_result,
        )


def _defer_admission(
    context: OrcaWorkerExecutionContext,
    queue_root: Path,
    *,
    reason: str,
) -> int:
    """Return a job that never started to the queue instead of failing it.

    The refusal was raised before the run wrote any state, so the generation is
    still pristine and the next claim reuses it without a recovery rebind. This
    re-asks for a resource; it never reruns a calculation.
    """
    queue_id = queue_entry_id(context.entry)
    requeued = requeue_running_entry(
        queue_root,
        queue_id,
        expected_entry=context.entry,
        admission_deferral_reason=reason,
    )
    if requeued:
        logger.warning("ORCA job %s waits for RAM scratch capacity: %s", queue_id, reason)
    else:
        logger.error(
            "ORCA job %s could not be returned to the queue after a RAM scratch "
            "capacity refusal: %s",
            queue_id,
            reason,
        )
    # Never zero: if the requeue was fenced out the row is still running, and
    # the parent must mark it failed rather than completed.
    return ADMISSION_DEFERRED_EXIT_CODE


def _record_worker_rejection(
    queue_root: Path,
    entry: QueueEntry,
    *,
    reason: str,
    expected_entry: QueueEntry | None = None,
) -> None:
    """Leave a fail-closed prelaunch rejection on the queue row before the child exits.

    The rejection ends the child before any engine runs, so without this the
    parent would only record ``exit_code=1``; the reason itself would survive in
    the journal alone. The rebind may already have written its durable claim
    onto the row, so recovery uses the row as it stands now. A fresh claim
    supplies its original publication generation instead. In both cases the
    mark is additionally fenced under the queue lock to the dequeue this child
    was handed: ``started_at`` is cleared by a requeue and re-stamped by every
    re-dequeue, while the rebind never touches it. ``mark_failed`` also refuses
    a row whose cancellation has already been requested, so a racing
    cancellation still wins.
    """

    queue_id = str(entry.queue_id)
    current = _queue_entry_by_id(queue_root, queue_id)
    recorded = current is not None and mark_failed(
        queue_root,
        queue_id,
        error=reason,
        publish_terminal_side_effects=not queue_entry_is_retired_workflow_owned(entry, queue_root),
        expected_entry=current if expected_entry is None else expected_entry,
        require_running_started_at=str(entry.started_at),
    )
    if not recorded:
        logger.info(
            "Worker rejection for %s left to the parent finalizer: %s",
            queue_id,
            reason,
        )


def _cancellation_requested(probe: Callable[[], bool]) -> bool:
    try:
        return probe()
    except QueueLockTimeoutError:
        return False


def process_dequeued_entry(
    cfg: AppConfig,
    entry: QueueEntry,
    *,
    queue_root: Path,
    admission_token: str | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
) -> OrcaWorkerExecutionOutcome:
    probe = cancellation_probe(queue_root, entry)
    try:
        context = _build_execution_context(cfg, entry, admission_token=admission_token)
    except (ValueError, OSError) as exc:
        _record_worker_rejection(
            queue_root,
            entry,
            reason=f"execution rejected: {exc}",
            expected_entry=entry,
        )
        raise
    if shutdown_requested is not None and shutdown_requested():
        raise WorkerShutdownRequested(context)
    result = _run_orca_job_for_entry(
        cfg,
        context,
        queue_root,
        should_cancel=lambda: _cancellation_requested(probe),
        shutdown_requested=shutdown_requested,
    )
    return OrcaWorkerExecutionOutcome(
        exit_code=int(result),
        reaction_dir=context.reaction_dir,
        entry=entry,
    )


def run_worker_child_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    await_parent_admission_handoff_fn: Callable[
        [str | Path, str], bool
    ] = await_parent_admission_handoff,
) -> int:
    controller = ChildWorkerShutdownController()
    cfg = load_config(config_path)
    resolved_queue_root = Path(queue_root).expanduser().resolve()
    entry = _queue_entry_by_id(resolved_queue_root, queue_id)
    if entry is not None and entry_matches_engine_identity(entry, "orca"):
        try:
            entry = _maybe_rebind_recovery_generation(
                entry,
                queue_root=resolved_queue_root,
                cfg_factory=lambda: load_config(config_path),
            )
        except (ValueError, FileExistsError) as exc:
            _record_worker_rejection(
                resolved_queue_root,
                entry,
                reason=f"crash recovery rejected: {exc}",
            )
            raise
    if entry is None or not (
        entry_status_is_running(entry) and entry_matches_engine_identity(entry, "orca")
    ):
        return 1
    if admission_token and not await_parent_admission_handoff_fn(
        resolve_admission_root(cfg),
        admission_token,
    ):
        return 1
    install_shutdown_signal_handlers(controller.request)
    # The parent owns final admission release, including shutdown and exceptions
    # between engine launch and publication of its durable process identity.
    try:
        outcome = process_dequeued_entry(
            cfg,
            entry,
            queue_root=resolved_queue_root,
            admission_token=admission_token,
            shutdown_requested=controller.is_requested,
        )
    except WorkerShutdownRequested:
        requeue_running_entry(
            resolved_queue_root,
            queue_id,
            expected_entry=entry,
            expected_task_id=queue_entry_task_id(entry) or None,
        )
        return 0
    return int(outcome.exit_code)


__all__ = [
    "BackgroundRunJobProcess",
    "OrcaWorkerExecutionContext",
    "OrcaWorkerExecutionOutcome",
    "WORKER_JOB_MODULE",
    "build_worker_child_command",
    "process_dequeued_entry",
    "run_worker_child_job",
]
