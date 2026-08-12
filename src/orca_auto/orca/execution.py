from __future__ import annotations

import logging
import re
from collections.abc import Mapping
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
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.messaging import build_channel
from orca_auto.core.utils.process_tracking import run_lock_status

from .attempt.engine import _exit_with_result, run_attempts
from .completion_rules import detect_completion_mode
from .config import load_config
from .notifications import (
    notify_retry_event,
    notify_run_finished_event,
    notify_run_started_event,
)
from .orca_runner import OrcaRunner
from .out_analyzer import analyze_output
from .retry_policy import retry_input_path
from .run_context import RunExecutionContext, resolve_execution_context
from .runtime.run_lock import LOCK_FILE_NAME, acquire_run_lock
from .scratch import OrcaScratchPolicy
from .state import load_state, save_state
from .state_machine import RESUMABLE_RUN_STATUSES, load_or_create_state
from .statuses import AnalyzerStatus, RunStatus

RETRY_INP_RE = re.compile(r"\.retry\d+$", re.IGNORECASE)
ORCA_GENERATED_INP_RE = re.compile(
    r"\.(scfgrad|scfhess|cis|autoci|cipsi|mrci|mdci|eprnmr|loc|nbo|compound|hess)"
    r"(\.retry\d+)?$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def select_latest_inp(reaction_dir: Path) -> Path:
    resolved_reaction_dir = reaction_dir.expanduser().resolve()
    all_candidates = list(reaction_dir.glob("*.inp"))
    if not all_candidates:
        raise ValueError(f"No .inp file found in: {reaction_dir}")
    # Prefer user-authored base inputs over generated retry/intermediate files.
    candidates = [
        p
        for p in all_candidates
        if not RETRY_INP_RE.search(p.stem) and not ORCA_GENERATED_INP_RE.search(p.stem)
    ]
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


def retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    return retry_input_path(selected_inp, retry_number)


def _to_resolved_local(path_text: str) -> Path:
    return Path(path_text).expanduser().resolve()


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


def existing_completed_out(selected_inp: Path) -> dict[str, Any] | None:
    base_stem = RETRY_INP_RE.sub("", selected_inp.stem)
    if not base_stem:
        base_stem = selected_inp.stem

    out_candidates = list(selected_inp.parent.glob(f"{base_stem}.out"))
    out_candidates.extend(selected_inp.parent.glob(f"{base_stem}.retry*.out"))
    out_candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name.lower()), reverse=True)

    seen: set[Path] = set()
    for out_path in out_candidates:
        resolved = out_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)

        mode_inp = out_path.with_suffix(".inp")
        if not mode_inp.exists():
            mode_inp = selected_inp
        if _out_is_older_than_inputs(out_path, selected_inp=selected_inp, mode_inp=mode_inp):
            continue
        mode = detect_completion_mode(mode_inp)
        analysis = analyze_output(out_path, mode)
        if analysis.status != AnalyzerStatus.COMPLETED:
            continue
        return {
            "out_path": str(out_path),
            "analysis": analysis,
        }
    return None


def _out_is_older_than_inputs(out_path: Path, *, selected_inp: Path, mode_inp: Path) -> bool:
    newest_input_mtime_ns = max(selected_inp.stat().st_mtime_ns, mode_inp.stat().st_mtime_ns)
    return out_path.stat().st_mtime_ns < newest_input_mtime_ns


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
    from .state import save_state

    save_state(reaction_dir, state)
    return True


def active_direct_run_error(reaction_dir: Path, *, logger: logging.Logger) -> str | None:
    status = run_lock_status(
        reaction_dir,
        logger=logger,
        lock_file_name=LOCK_FILE_NAME,
    )
    if not status.held:
        return None

    owner = f"pid={status.pid}" if status.pid is not None else "pid=unknown"
    started = status.started_at or "unknown"
    return (
        "Another orca_auto instance is already running in this directory "
        f"({owner}, started_at={started}). Lock file: {reaction_dir / LOCK_FILE_NAME}"
    )


def notification_callbacks(cfg: Any) -> tuple[Any, Any, Any]:
    channel = build_channel(cfg.messenger, logger=logging.getLogger(__name__))
    if not channel.enabled:
        return None, None, None

    def notify_started(event: Any) -> bool:
        return notify_run_started_event(channel, event)

    def notify_finished(event: Any) -> bool:
        return notify_run_finished_event(channel, event)

    def notify_retry(event: Any) -> bool:
        return notify_retry_event(channel, event)

    return notify_started, notify_finished, notify_retry


def run_with_state(
    *,
    cfg: Any,
    reaction_dir: Path,
    selected_inp: Path,
    runner_cls: type[Any],
    max_retries: int,
    resumed: bool,
    state: Any,
    admission_root: Path | None = None,
    reservation_token: str | None = None,
) -> int:
    notify_started, notify_finished, notify_retry = notification_callbacks(cfg)
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
        if callable(set_registrar):
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
    # Carry the per-task memory budget into the run so retry rewrites can cap
    # escalated %maxcore (see inp_rewriter.rewrite_for_retry).
    state["max_memory_gb_per_task"] = int(cfg.resources.max_memory_gb_per_task)
    return run_attempts(
        reaction_dir,
        selected_inp,
        state,
        resumed=resumed,
        runner=runner,
        max_retries=max_retries,
        retry_inp_path=retry_inp_path,
        to_resolved_local=_to_resolved_local,
        emit=_emit,
        notify_started=notify_started,
        notify_finished=notify_finished,
        notify_retry=notify_retry,
    )


def existing_completed_exit(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    admission_root: Path,
    reservation_token: str | None,
    max_retries: int,
    admission_task_id: str | None,
    execution_provenance: Mapping[str, Any] | None = None,
    queue_id: str | None = None,
    queue_generation: str | None = None,
) -> int | None:
    del admission_root, reservation_token
    done = existing_completed_out(selected_inp)
    if done is None:
        return None

    state, resumed = load_or_create_state(
        reaction_dir,
        selected_inp,
        max_retries=max_retries,
        to_resolved_local=_to_resolved_local,
    )
    state_changed = False
    if execution_provenance and state.get("execution_provenance") != dict(execution_provenance):
        state["execution_provenance"] = dict(execution_provenance)
        state_changed = True
    task_id = str(admission_task_id or "").strip()
    if task_id and state.get("job_id") != task_id:
        # A queued child may discover an already-completed output before the
        # ordinary state-loading path below runs. Stamp the queue task ID
        # first so the terminal replay can bind the resulting artifacts to
        # this queue generation.
        state["job_id"] = task_id
        state_changed = True
    resolved_queue_id = str(queue_id or "").strip()
    if resolved_queue_id and state.get("queue_id") != resolved_queue_id:
        state["queue_id"] = resolved_queue_id
        state_changed = True
    resolved_queue_generation = str(queue_generation or "").strip()
    if resolved_queue_generation and state.get("queue_generation") != resolved_queue_generation:
        state["queue_generation"] = resolved_queue_generation
        state_changed = True
    if state_changed:
        save_state(reaction_dir, state)
    return _exit_with_result(
        reaction_dir,
        state,
        selected_inp,
        status=RunStatus.COMPLETED,
        analyzer_status=AnalyzerStatus.COMPLETED,
        reason="existing_out_completed",
        last_out_path=done["out_path"],
        resumed=True if resumed else None,
        exit_code=0,
        emit=_emit,
        extra={"skipped_execution": True},
    )


def execute_locked_run(
    args: Any,
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
            context_provenance = getattr(context, "execution_provenance", None)
            context_queue_id = str(getattr(context, "queue_id", "") or "").strip()
            context_queue_generation = str(getattr(context, "queue_generation", "") or "").strip()
            if not getattr(args, "force", False):
                existing_kwargs: dict[str, Any] = {
                    "reaction_dir": context.reaction_dir,
                    "selected_inp": context.selected_inp,
                    "admission_root": context.admission_root,
                    "reservation_token": context.reservation_token,
                    "max_retries": context.max_retries,
                    "admission_task_id": context.admission_task_id,
                }
                if context_provenance:
                    existing_kwargs["execution_provenance"] = context_provenance
                if context_queue_id:
                    existing_kwargs["queue_id"] = context_queue_id
                if context_queue_generation:
                    existing_kwargs["queue_generation"] = context_queue_generation
                existing_exit = existing_completed_exit(
                    **existing_kwargs,
                )
                if existing_exit is not None:
                    return existing_exit

            state, resumed = load_or_create_state(
                context.reaction_dir,
                context.selected_inp,
                max_retries=context.max_retries,
                to_resolved_local=_to_resolved_local,
            )
            state_changed = False
            if context_provenance and state.get("execution_provenance") != dict(context_provenance):
                state["execution_provenance"] = dict(context_provenance)
                state_changed = True
            if context.admission_task_id and state.get("job_id") != context.admission_task_id:
                state["job_id"] = context.admission_task_id
                state_changed = True
            if context_queue_id and state.get("queue_id") != context_queue_id:
                state["queue_id"] = context_queue_id
                state_changed = True
            if (
                context_queue_generation
                and state.get("queue_generation") != context_queue_generation
            ):
                state["queue_generation"] = context_queue_generation
                state_changed = True
            if state_changed:
                save_state(context.reaction_dir, state)
            return run_with_state(
                cfg=context.cfg,
                reaction_dir=context.reaction_dir,
                selected_inp=context.selected_inp,
                runner_cls=runner_cls,
                max_retries=context.max_retries,
                resumed=resumed,
                state=state,
                admission_root=context.admission_root,
                reservation_token=context.reservation_token,
            )


def execute_orca_run(
    args: Any,
    *,
    runner_cls: type[Any] = OrcaRunner,
    cfg: Any | None = None,
    reaction_dir: Path | None = None,
    selected_inp: Path | None = None,
    reservation_token: str | None = None,
    admission_app_name: str | None = None,
    admission_task_id: str | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
    queue_id: str | None = None,
    queue_generation: str | None = None,
    logger: logging.Logger | None = None,
) -> int:
    logger = logger or logging.getLogger(__name__)
    context = resolve_execution_context(
        args,
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        reservation_token=reservation_token,
        admission_app_name=admission_app_name,
        admission_task_id=admission_task_id,
        execution_provenance=execution_provenance,
        queue_id=queue_id,
        queue_generation=queue_generation,
        load_config_fn=load_config,
        select_latest_inp_fn=select_latest_inp,
        logger=logger,
    )
    if context is None:
        return 1

    logger.info("Selected input: %s", context.selected_inp)

    try:
        # Run-state recovery stays inside the reaction lock. Engine-process
        # ownership is reconciled independently through the admission store.
        return execute_locked_run(args, context, runner_cls=runner_cls)
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
