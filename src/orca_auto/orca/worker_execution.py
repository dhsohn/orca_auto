from __future__ import annotations

import argparse
import copy
import subprocess
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.admission import release_slot
from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.engines.worker_child import (
    WORKER_CHILD_MODULE,
    build_worker_child_command_for_engine,
)
from orca_auto.core.queue.child.execution import find_queue_entry_by_id
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.internal_engine import (
    InternalEngineSpec,
    entry_matches_engine_identity,
    entry_status_is_running,
)
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
)
from orca_auto.core.queue.worker.execution_dependencies import run_worker_child_entrypoint

from .attempt.reporting import build_final_result
from .commands.run_inp import _cmd_run_inp_execute
from .config import load_config
from .execution_binding import orca_execution_provenance, verify_orca_execution_snapshot
from .orca_runner import OrcaRunner, WorkerShutdownInterrupt
from .queue.adapter import (
    get_cancel_requested,
    list_queue,
    queue_entry_app_name,
    queue_entry_force,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    requeue_running_entry,
)
from .state import finalize_state, load_state
from .statuses import AnalyzerStatus

BackgroundRunJobProcess = subprocess.Popen
WORKER_JOB_MODULE = WORKER_CHILD_MODULE
_ENGINE_SPEC = InternalEngineSpec(
    engine="orca",
    worker_job_module=WORKER_CHILD_MODULE,
    include_admission_root=False,
)


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


_worker_child = _ENGINE_SPEC.worker_child_module_facade(
    WorkerShutdownRequested,
    entry_ready_fn=lambda entry: (
        entry_status_is_running(entry) and entry_matches_engine_identity(entry, "orca")
    ),
    outcome_exit_code_fn=_orca_worker_outcome_exit_code,
    build_worker_child_command=build_worker_child_command,
)
_WORKER_CHILD = _worker_child.worker_child


def _canonical_admission_app_name(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text == ORCA_AUTO_ORCA_APP_NAME:
        return ORCA_AUTO_ORCA_APP_NAME
    return text


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
    verified_selected, orca_executable = verify_orca_execution_snapshot(
        reaction_dir,
        snapshot,
        expected_selected_inp=selected_inp,
        expected_source_selected_inp=source_selected_inp,
        expected_selected_input_xyz=selected_input_xyz,
        expected_resource_request=resource_request,
        expected_max_retries=max_retries,
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
    _options: _engine_execution.InternalWorkerOptions,
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
            self.set_shutdown_requested(stop_requested)

        def run(self, inp_path: Path) -> Any:
            try:
                current_input = require_confined_regular_file(
                    Path(context.execution_snapshot["execution_dir"]),
                    inp_path,
                    label="ORCA queued execution input",
                )
                if (
                    current_input.parent
                    != Path(context.execution_snapshot["execution_dir"]).resolve()
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
                    result.execution_provenance = dict(execution_provenance)
                    return result
                finally:
                    self._runtime_outputs_started = True
            finally:
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

    bound_cfg = copy.copy(cfg)
    bound_cfg.runtime = copy.copy(cfg.runtime)
    bound_cfg.runtime.default_max_retries = context.max_retries
    if hasattr(cfg, "resources"):
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
                    last_out_path=None,
                )
                finalize_state(
                    Path(context.reaction_dir),
                    state,
                    status="cancelled",
                    final_result=cancelled_result,
                )
        raise WorkerShutdownRequested(context) from exc


def _worker_execution_spec(
    *,
    worker_config_path: str,
    admission_token: str | None,
) -> _engine_execution.InternalEngineWorkerExecutionSpec:
    return _engine_execution.build_internal_engine_worker_execution_spec(
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
    return _engine_execution.run_internal_engine_worker_entry_with_spec_factory_options(
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
    return run_worker_child_entrypoint(
        _worker_child,
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        admission_token=admission_token,
        load_config_fn=load_config,
        find_queue_entry_fn=_queue_entry_by_id,
        admission_root_fn=resolve_admission_root,
        release_slot_fn=release_slot,
        install_shutdown_signal_handlers_fn=install_shutdown_signal_handlers,
        process_dequeued_entry_fn=process_dequeued_entry,
        dependencies_fn=lambda: None,
        requeue_running_entry_fn=requeue_running_entry,
        mark_recovery_pending_context_fn=_mark_recovery_pending_context,
        process_dequeued_entry_kwargs={
            "worker_config_path": config_path,
            "admission_token": admission_token,
        },
        await_parent_admission_handoff_fn=await_parent_admission_handoff_fn,
    )


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
    runner_cls: type[OrcaRunner] = OrcaRunner,
) -> int:
    return _cmd_run_inp_execute(
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
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {WORKER_JOB_MODULE}")
    parser.add_argument("--config", required=True)
    parser.add_argument("--queue-root", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--admission-token", default=None)
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
