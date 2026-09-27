"""Run one selected ORCA input to a terminal result under the reaction lock.

``execute_orca_run`` is the single entry point: it takes the reaction lock,
recovers a crashed resumable state, activates the queue's admission slot
(``_child_admission_slot``, the child's one slot rule), settles from an already
completed output when ``output_adoption`` says one exists, reserves RAM
scratch, and otherwise loads (or creates) ``job_state.json`` and drives
``attempt.engine.run_attempts`` with a runner built for the configured scratch
and admission registrars.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    AdmissionLimitReachedError,
    activate_reserved_slot,
    build_slot_engine_process_preparer,
    build_slot_engine_process_registrar,
    complete_slot_engine_process,
    release_slot,
)
from orca_auto.core.admission.records import ADMISSION_SOURCE_QUEUE_RUN, SLOT_STATE_ACTIVE
from orca_auto.core.engine_scratch import EngineScratchCapacityError

from .attempt.engine import run_attempts
from .notifications import (
    notification_channel,
    notify_run_started_event,
)
from .orca_runner import OrcaRunner
from .output_adoption import existing_completed_exit, to_resolved_local
from .run_context import RunExecutionContext, bind_queue_identity
from .run_lock import acquire_run_lock
from .scratch import OrcaScratchPolicy
from .state import RESUMABLE_RUN_STATUSES, load_or_create_state, save_state
from .state_reading import load_state
from .statuses import AnalyzerStatus, RunStatus
from .types import RunStartedNotification

logger = logging.getLogger(__name__)


def _emit(payload: dict[str, Any]) -> None:
    fields = [
        ("status", "status"),
        ("job_dir", "job_dir"),
        ("reaction_dir", "job_dir"),
        ("selected_inp", "selected_inp"),
        ("attempt_count", "attempt_count"),
        ("reason", "reason"),
        ("run_state", "run_state"),
        ("report_json", "report_json"),
    ]
    emitted_labels: set[str] = set()
    for key, label in fields:
        if key not in payload or label in emitted_labels:
            continue
        print(f"{label}: {payload[key]}")
        emitted_labels.add(label)


@contextmanager
def _child_admission_slot(context: RunExecutionContext) -> Iterator[None]:
    """Every admission slot mutation the worker child makes, under one rule.

    The parent reserves the slot and attaches this child to it; the child
    activates it for its run directory. When the run returns, the child
    completes the engine process: a launch fence left pending goes back to
    idle, and a slot that still records an active engine fails the run. When
    the run raises, the child leaves the slot as it is. Either way the parent
    recovers any engine record and releases the slot after the child exits.
    The child releases only a slot that activation no longer finds live.
    """
    token = context.admission_token
    if not token:
        raise AdmissionLimitReachedError(
            "ORCA execution requires a queue admission reservation. "
            "Submit the directory with `orca_auto run-dir` and let the queue worker execute it."
        )
    activated = activate_reserved_slot(
        context.admission_root,
        token,
        state=SLOT_STATE_ACTIVE,
        work_dir=context.reaction_dir,
        source=ADMISSION_SOURCE_QUEUE_RUN,
        app_name=context.admission_app_name,
        task_id=context.admission_task_id,
    )
    if activated is None:
        release_slot(context.admission_root, token)
        raise AdmissionLimitReachedError(
            f"Failed to activate reserved admission slot for {context.reaction_dir}."
        )
    yield
    if complete_slot_engine_process(context.admission_root, token) is None:
        raise RuntimeError(f"Admission slot disappeared: {token}")


def recover_crashed_state(reaction_dir: Path, *, logger: logging.Logger) -> bool:
    """Recover resumable run state after admission has reconciled engine ownership."""
    state = load_state(reaction_dir)
    if not state:
        return False

    status = str(state.get("status", "")).strip()
    if status not in RESUMABLE_RUN_STATUSES:
        return False

    logger.warning(
        "Detected crashed run in %s (status=%s). Recovering state.",
        reaction_dir,
        status,
    )
    state["status"] = RunStatus.FAILED.value
    state["final_result"] = {
        "status": RunStatus.FAILED.value,
        "reason": "crashed_recovery",
        "analyzer_status": AnalyzerStatus.INCOMPLETE.value,
    }
    save_state(reaction_dir, state)
    return True


def started_notification_callback(cfg: Any) -> Callable[[RunStartedNotification], bool] | None:
    channel = notification_channel(cfg)
    if not channel.enabled:
        return None

    def notify_started(event: RunStartedNotification) -> bool:
        return notify_run_started_event(channel, event)

    return notify_started


def _build_runner(context: RunExecutionContext, *, runner_cls: type[Any]) -> Any:
    cfg = context.cfg
    runner = runner_cls(cfg.paths.orca_executable)
    if cfg.scratch.enabled:
        set_scratch_policy = getattr(runner, "set_scratch_policy", None)
        if not callable(set_scratch_policy):
            raise TypeError("Configured ORCA runner does not support RAM scratch execution")
        set_scratch_policy(
            OrcaScratchPolicy(
                root=Path(cfg.scratch.root),
                min_free_bytes=int(cfg.scratch.min_free_gb) * 1024**3,
                max_task_memory_bytes=int(context.resource_request["max_memory_gb"]) * 1024**3,
            )
        )
    if context.admission_token:
        set_registrar = getattr(runner, "set_running_job_registrar", None)
        if not callable(set_registrar):
            raise TypeError("Admitted ORCA runner does not support engine-process registration")
        set_registrar(
            build_slot_engine_process_registrar(context.admission_root, context.admission_token),
            prepare=build_slot_engine_process_preparer(
                context.admission_root, context.admission_token
            ),
        )
    return runner


def run_with_state(
    context: RunExecutionContext,
    *,
    runner_cls: type[Any],
    resumed: bool,
    state: Any,
    runner: Any | None,
) -> int:
    notify_started = started_notification_callback(context.cfg)
    if runner is None:
        runner = _build_runner(context, runner_cls=runner_cls)
    return run_attempts(
        context.reaction_dir,
        context.selected_inp,
        state,
        resumed=resumed,
        runner=runner,
        emit=_emit,
        notify_started=notify_started,
    )


def execute_locked_run(
    context: RunExecutionContext,
    *,
    runner_cls: type[Any],
) -> int:
    with acquire_run_lock(context.reaction_dir):
        recover_crashed_state(context.reaction_dir, logger=logger)
        with _child_admission_slot(context):
            # The probe reads only the bound input's own generation directory:
            # a crashed generation adopts its own output, a fresh one runs.
            existing_exit = existing_completed_exit(context, emit=_emit)
            if existing_exit is not None:
                return existing_exit

            with _prepared_scratch_runner(context, runner_cls=runner_cls) as runner:
                return _load_state_and_run(context, runner_cls=runner_cls, runner=runner)


@contextmanager
def _prepared_scratch_runner(context: RunExecutionContext, *, runner_cls: type[Any]) -> Any:
    """Reserve RAM scratch before the run writes its first state.

    A capacity refusal raised here leaves no state, attempt record,
    notification or generation artifact, so the job was never started and may
    wait for admission again. Once state exists the same refusal is a failed
    attempt, which is never rerun.
    """
    if not getattr(getattr(context.cfg, "scratch", None), "enabled", False):
        yield None
        return
    runner = _build_runner(context, runner_cls=runner_cls)
    try:
        runner.prepare(context.selected_inp)
        yield runner
    finally:
        runner.release_prepared()


def _load_state_and_run(
    context: RunExecutionContext,
    *,
    runner_cls: type[Any],
    runner: Any | None,
) -> int:
    state, resumed = load_or_create_state(
        context.reaction_dir,
        context.selected_inp,
        to_resolved_local=to_resolved_local,
    )
    if bind_queue_identity(state, context):
        save_state(context.reaction_dir, state)
    return run_with_state(
        context, runner_cls=runner_cls, resumed=resumed, state=state, runner=runner
    )


def execute_orca_run(
    context: RunExecutionContext,
    *,
    runner_cls: type[Any] = OrcaRunner,
    logger: logging.Logger | None = None,
) -> int:
    logger = logger or logging.getLogger(__name__)
    logger.info("Selected input: %s", context.selected_inp)

    try:
        # Run-state recovery stays inside the reaction lock. Engine-process
        # ownership is reconciled independently through the admission store.
        return execute_locked_run(context, runner_cls=runner_cls)
    except EngineScratchCapacityError:
        # Raised only by the preparation that precedes this run's first state
        # write; the queue child decides whether the job waits. It must not
        # become an ordinary failure here.
        raise
    except RuntimeError as exc:
        # Includes AdmissionLimitReachedError.
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        logger.exception("Unexpected error while running input: %s", exc)
        return 1
