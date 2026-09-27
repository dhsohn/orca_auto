"""Run one selected ORCA input to a terminal result under the reaction lock.

``execute_orca_run`` is the single entry point and maps failures to the exit
code; ``execute_locked_run`` is the run itself, in order: take the reaction
lock, recover a crashed resumable state, activate the admission slot
(``_child_admission_slot``, the child's one slot rule), settle from an already
completed output when ``output_adoption`` says one exists, and otherwise build
the one ``OrcaRunner``, reserve RAM scratch, load (or create) and bind
``job_state.json`` and make the run's one attempt (``attempt.run.run_attempt``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

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

from .attempt.resume import CRASHED_RECOVERY_REASON, RESUMABLE_RUN_STATUSES, load_or_create_state
from .attempt.run import run_attempt
from .orca_runner import OrcaRunner
from .output_adoption import existing_completed_exit
from .run_context import RunExecutionContext, bind_queue_identity
from .run_lock import acquire_run_lock
from .scratch import OrcaScratchPolicy
from .state import save_state
from .state_reading import load_state
from .statuses import AnalyzerStatus, RunStatus

logger = logging.getLogger(__name__)


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
        "reason": CRASHED_RECOVERY_REASON,
        "analyzer_status": AnalyzerStatus.INCOMPLETE.value,
    }
    save_state(reaction_dir, state)
    return True


def execute_locked_run(
    context: RunExecutionContext,
    *,
    stop_requested: Callable[[], bool],
) -> int:
    with acquire_run_lock(context.reaction_dir):
        recover_crashed_state(context.reaction_dir, logger=logger)
        with _child_admission_slot(context):
            # The probe reads only the bound input's own generation directory:
            # a crashed generation adopts its own output, a fresh one runs.
            existing_exit = existing_completed_exit(context)
            if existing_exit is not None:
                return existing_exit

            cfg = context.cfg
            snapshot = context.execution_snapshot
            runner = OrcaRunner(
                context.orca_executable,
                executable_identity=snapshot["executable_identities"]["orca"],
                execution_dir=Path(snapshot["execution_dir"]),
                execution_dir_identity=snapshot["execution_dir_identity"],
                execution_provenance=context.execution_provenance,
                verify_snapshot=context.verify_snapshot,
                stop_requested=stop_requested,
                scratch_policy=(
                    OrcaScratchPolicy(
                        root=Path(cfg.scratch.root),
                        min_free_bytes=int(cfg.scratch.min_free_gb) * 1024**3,
                        max_task_memory_bytes=int(context.resource_request["max_memory_gb"])
                        * 1024**3,
                    )
                    if cfg.scratch.enabled
                    else None
                ),
                prepare_running_job=build_slot_engine_process_preparer(
                    context.admission_root, context.admission_token
                ),
                register_running_job=build_slot_engine_process_registrar(
                    context.admission_root, context.admission_token
                ),
            )
            try:
                # RAM scratch is reserved before the first state write. A
                # capacity refusal here leaves no state, attempt record,
                # notification or generation artifact, so the job never started
                # and may wait for admission again. Once state exists the same
                # refusal is a failed attempt, which is never rerun.
                runner.prepare(context.selected_inp)
                state, resumed = load_or_create_state(context.reaction_dir, context.selected_inp)
                if bind_queue_identity(state, context):
                    save_state(context.reaction_dir, state)
                return run_attempt(context, state, runner, resumed=resumed)
            finally:
                runner.release_prepared()


def execute_orca_run(
    context: RunExecutionContext,
    *,
    stop_requested: Callable[[], bool],
) -> int:
    logger.info("Selected input: %s", context.selected_inp)

    try:
        # Run-state recovery stays inside the reaction lock. Engine-process
        # ownership is reconciled independently through the admission store.
        return execute_locked_run(context, stop_requested=stop_requested)
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
