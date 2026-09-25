"""Run one selected ORCA input to a terminal result under the reaction lock.

``execute_orca_run`` is the single entry point: it takes the reaction lock,
recovers a crashed resumable state, activates the queue's admission
reservation, settles from an already completed output when
``output_adoption`` says one exists, reserves RAM scratch, and otherwise
loads (or creates) ``job_state.json`` and drives ``attempt.engine.run_attempts``
with a runner built for the configured scratch and admission registrars.
Admission-related failures release the reservation before they are reported.
Input selection (``select_latest_inp``) and the direct-run lock probe
(``active_direct_run_error``) live here because submission shares them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    AdmissionLimitReachedError,
    build_slot_engine_process_preparer,
    build_slot_engine_process_registrar,
    complete_slot_engine_process,
    get_slot,
    release_slot,
)
from orca_auto.core.admission import activate_reserved_slot as _activate_reserved_slot
from orca_auto.core.confined_io import require_confined_regular_file
from orca_auto.core.engine_scratch import EngineScratchCapacityError
from orca_auto.core.utils.process_tracking import RUN_LOCK_FILE_NAME, run_lock_status

from .attempt.engine import run_attempts
from .notifications import (
    notification_channel,
    notify_run_started_event,
)
from .orca_runner import OrcaRunner
from .output_adoption import existing_completed_exit, to_resolved_local
from .output_adoption import existing_completed_out as existing_completed_out
from .run_context import RunExecutionContext
from .run_lock import acquire_run_lock
from .scratch import OrcaScratchPolicy
from .state import RESUMABLE_RUN_STATUSES, load_or_create_state, save_state
from .state_reading import load_state
from .statuses import AnalyzerStatus, RunStatus
from .types import RunStartedNotification

ORCA_GENERATED_INP_RE = re.compile(
    r"\.(scfgrad|scfhess|cis|autoci|cipsi|mrci|mdci|eprnmr|loc|nbo|compound|hess)"
    r"$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


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
        logging.getLogger(__name__).warning(
            "Multiple ORCA .inp candidates found in %s; selected newest input %s",
            reaction_dir,
            candidates[0].name,
        )
    return candidates[0]


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


def _release_reservation_if_needed(admission_root: Path, reservation_token: str | None) -> None:
    if reservation_token is None:
        return
    slot = get_slot(admission_root, reservation_token)
    if slot is None or slot.engine_process_state:
        return
    release_slot(admission_root, reservation_token)


@contextmanager
def _activated_reserved_slot_context(
    admission_root: Path,
    reservation_token: str,
    *,
    reaction_dir: Path,
    source: str,
    app_name: str | None,
    task_id: str | None,
) -> Any:
    activated = _activate_reserved_slot(
        admission_root,
        reservation_token,
        state="active",
        work_dir=reaction_dir,
        source=source,
        app_name=app_name,
        task_id=task_id,
    )
    if activated is None:
        release_slot(admission_root, reservation_token)
        raise AdmissionLimitReachedError(
            f"Failed to activate reserved admission slot for {reaction_dir}."
        )
    managed_slot = bool(getattr(activated, "engine_process_state", ""))

    try:
        yield reservation_token
    except BaseException:
        if not managed_slot:
            release_slot(admission_root, reservation_token)
        raise
    else:
        if managed_slot:
            completed = complete_slot_engine_process(admission_root, reservation_token)
            if completed is None:
                raise RuntimeError(f"Admission slot disappeared: {reservation_token}")
        else:
            release_slot(admission_root, reservation_token)


def _admission_context(
    *,
    admission_root: Path,
    reaction_dir: Path,
    reservation_token: str | None,
    admission_app_name: str | None,
    admission_task_id: str | None,
) -> AbstractContextManager[str]:
    if reservation_token is not None:
        return _activated_reserved_slot_context(
            admission_root,
            reservation_token,
            reaction_dir=reaction_dir,
            source="queue_run",
            app_name=admission_app_name,
            task_id=admission_task_id,
        )
    raise AdmissionLimitReachedError(
        "ORCA execution requires a queue admission reservation. "
        "Submit the directory with `orca_auto run-dir` and let the queue worker execute it."
    )


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


def active_direct_run_error(reaction_dir: Path, *, logger: logging.Logger) -> str | None:
    status = run_lock_status(
        reaction_dir,
        logger=logger,
        lock_file_name=RUN_LOCK_FILE_NAME,
    )
    if not status.held:
        return None

    owner = f"pid={status.pid}" if status.pid is not None else "pid=unknown"
    started = status.started_at or "unknown"
    return (
        "Another orca_auto instance is already running in this directory "
        f"({owner}, started_at={started}). Lock file: {reaction_dir / RUN_LOCK_FILE_NAME}"
    )


def started_notification_callback(cfg: Any) -> Callable[[RunStartedNotification], bool] | None:
    channel = notification_channel(cfg)
    if not channel.enabled:
        return None

    def notify_started(event: RunStartedNotification) -> bool:
        return notify_run_started_event(channel, event)

    return notify_started


def _build_runner(
    *,
    cfg: Any,
    runner_cls: type[Any],
    admission_root: Path | None,
    reservation_token: str | None,
) -> Any:
    runner = runner_cls(cfg.paths.orca_executable)
    if cfg.scratch.enabled:
        set_scratch_policy = getattr(runner, "set_scratch_policy", None)
        if not callable(set_scratch_policy):
            raise TypeError("Configured ORCA runner does not support RAM scratch execution")
        set_scratch_policy(
            OrcaScratchPolicy(
                root=Path(cfg.scratch.root),
                min_free_bytes=int(cfg.scratch.min_free_gb) * 1024**3,
                max_task_memory_bytes=int(cfg.resources.max_memory_gb_per_task) * 1024**3,
            )
        )
    if admission_root is not None and reservation_token:
        set_registrar = getattr(runner, "set_running_job_registrar", None)
        if not callable(set_registrar):
            raise TypeError("Admitted ORCA runner does not support engine-process registration")
        set_registrar(
            build_slot_engine_process_registrar(
                admission_root,
                reservation_token,
            ),
            prepare=build_slot_engine_process_preparer(
                admission_root,
                reservation_token,
            ),
        )
    return runner


def run_with_state(
    *,
    cfg: Any,
    reaction_dir: Path,
    selected_inp: Path,
    runner_cls: type[Any],
    resumed: bool,
    state: Any,
    admission_root: Path | None = None,
    reservation_token: str | None = None,
    runner: Any | None = None,
) -> int:
    notify_started = started_notification_callback(cfg)
    if runner is None:
        runner = _build_runner(
            cfg=cfg,
            runner_cls=runner_cls,
            admission_root=admission_root,
            reservation_token=reservation_token,
        )
    return run_attempts(
        reaction_dir,
        selected_inp,
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
        with _admission_context(
            admission_root=context.admission_root,
            reaction_dir=context.reaction_dir,
            reservation_token=context.reservation_token,
            admission_app_name=context.admission_app_name,
            admission_task_id=context.admission_task_id,
        ):
            if not context.force:
                existing_exit = existing_completed_exit(
                    reaction_dir=context.reaction_dir,
                    selected_inp=context.selected_inp,
                    admission_root=context.admission_root,
                    reservation_token=context.reservation_token,
                    admission_task_id=context.admission_task_id,
                    execution_provenance=context.execution_provenance,
                    queue_id=context.queue_id or "",
                    queue_generation=context.queue_generation or "",
                    emit=_emit,
                )
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
    runner = _build_runner(
        cfg=context.cfg,
        runner_cls=runner_cls,
        admission_root=context.admission_root,
        reservation_token=context.reservation_token,
    )
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
    state_changed = False
    if context.execution_provenance and state.get("execution_provenance") != dict(
        context.execution_provenance
    ):
        state["execution_provenance"] = dict(context.execution_provenance)
        state_changed = True
    if context.admission_task_id and state.get("job_id") != context.admission_task_id:
        state["job_id"] = context.admission_task_id
        state_changed = True
    if context.queue_id and state.get("queue_id") != context.queue_id:
        state["queue_id"] = context.queue_id
        state_changed = True
    if context.queue_generation and state.get("queue_generation") != context.queue_generation:
        state["queue_generation"] = context.queue_generation
        state_changed = True
    if state_changed:
        save_state(context.reaction_dir, state)
    return run_with_state(
        cfg=context.cfg,
        reaction_dir=context.reaction_dir,
        selected_inp=context.selected_inp,
        runner_cls=runner_cls,
        resumed=resumed,
        state=state,
        admission_root=context.admission_root,
        reservation_token=context.reservation_token,
        runner=runner,
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
        _release_reservation_if_needed(context.admission_root, context.reservation_token)
        raise
    except AdmissionLimitReachedError as exc:
        _release_reservation_if_needed(context.admission_root, context.reservation_token)
        logger.error("%s", exc)
        return 1
    except RuntimeError as exc:
        _release_reservation_if_needed(context.admission_root, context.reservation_token)
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        _release_reservation_if_needed(context.admission_root, context.reservation_token)
        logger.exception("Unexpected error while running input: %s", exc)
        return 1
