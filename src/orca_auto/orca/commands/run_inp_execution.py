from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.utils.process_lock import parse_lock_info
from orca_auto.core.utils.process_tracking import active_run_lock_pid

from ..completion_rules import detect_completion_mode
from ..orca_process import recover_orphaned_orca_process
from ..out_analyzer import analyze_output
from ..runtime.run_lock import LOCK_FILE_NAME
from ..state import load_state
from ..state_machine import RESUMABLE_RUN_STATUSES
from ..statuses import AnalyzerStatus, RunStatus
from ._helpers import ORCA_GENERATED_INP_RE, RETRY_INP_RE


def select_latest_inp(reaction_dir: Path) -> Path:
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
    candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name.lower()), reverse=True)
    if len(candidates) > 1:
        logging.getLogger(__name__).warning(
            "Multiple ORCA .inp candidates found in %s; selected newest input %s",
            reaction_dir,
            candidates[0].name,
        )
    return candidates[0]


def retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    base_stem = RETRY_INP_RE.sub("", selected_inp.stem)
    if not base_stem:
        base_stem = selected_inp.stem
    return selected_inp.with_name(f"{base_stem}.retry{retry_number:02d}.inp")


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
    """Detect and recover from a crashed run (status=running/retrying but no active lock)."""
    state = load_state(reaction_dir)
    if not state:
        return False

    status = str(state.get("status", "")).strip()
    if status not in RESUMABLE_RUN_STATUSES:
        return False

    lock_path = reaction_dir / LOCK_FILE_NAME
    if lock_path.exists() and active_run_lock_pid(
        reaction_dir,
        logger=logger,
        lock_file_name=LOCK_FILE_NAME,
    ):
        return False

    recover_orphaned_orca_process(reaction_dir, logger=logger)

    logger.warning(
        "Detected crashed run in %s (status=%s, no active lock). Recovering state.",
        reaction_dir,
        status,
    )
    state["status"] = RunStatus.FAILED.value
    state["final_result"] = {
        "status": RunStatus.FAILED.value,
        "reason": "crashed_recovery",
        "analyzer_status": AnalyzerStatus.INCOMPLETE.value,
    }
    from ..state import save_state

    save_state(reaction_dir, state)
    return True


def active_direct_run_error(reaction_dir: Path, *, logger: logging.Logger) -> str | None:
    lock_info = parse_lock_info(reaction_dir / LOCK_FILE_NAME)
    lock_pid = active_run_lock_pid(
        reaction_dir,
        logger=logger,
        lock_file_name=LOCK_FILE_NAME,
    )
    if lock_pid is None:
        return None

    started_at = lock_info.get("started_at")
    started = started_at if isinstance(started_at, str) and started_at else "unknown"
    return (
        "Another orca_auto instance is already running in this directory "
        f"(pid={lock_pid}, started_at={started}). Lock file: {reaction_dir / LOCK_FILE_NAME}"
    )


def notification_callbacks(cfg: Any, *, deps: Any) -> tuple[Any, Any, Any]:
    if not cfg.telegram.enabled:
        return None, None, None
    notifications = deps.notifications

    def notify_started(event: Any) -> bool:
        return notifications.notify_run_started_event(cfg.telegram, event)

    def notify_finished(event: Any) -> bool:
        return notifications.notify_run_finished_event(cfg.telegram, event)

    def notify_retry(event: Any) -> bool:
        return notifications.notify_retry_event(cfg.telegram, event)

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
    deps: Any,
) -> int:
    execution = deps.execution
    notify_started, notify_finished, notify_retry = execution._notification_callbacks(cfg)
    runner = runner_cls(cfg.paths.orca_executable)
    # Carry the per-task memory budget into the run so retry rewrites can cap
    # escalated %maxcore (see inp_rewriter.rewrite_for_retry).
    state["max_memory_gb_per_task"] = int(cfg.resources.max_memory_gb_per_task)
    return execution.run_attempts(
        reaction_dir,
        selected_inp,
        state,
        resumed=resumed,
        runner=runner,
        max_retries=max_retries,
        retry_inp_path=execution._retry_inp_path,
        to_resolved_local=execution._to_resolved_local,
        emit=execution._emit,
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
    deps: Any,
) -> int | None:
    del admission_root, reservation_token
    execution = deps.execution
    statuses = deps.statuses
    done = execution._existing_completed_out(selected_inp)
    if done is None:
        return None

    state, resumed = execution.load_or_create_state(
        reaction_dir,
        selected_inp,
        max_retries=max_retries,
        to_resolved_local=execution._to_resolved_local,
    )
    return execution._exit_with_result(
        reaction_dir,
        state,
        selected_inp,
        status=statuses.RunStatus.COMPLETED,
        analyzer_status=statuses.AnalyzerStatus.COMPLETED,
        reason="existing_out_completed",
        last_out_path=done["out_path"],
        resumed=True if resumed else None,
        exit_code=0,
        emit=execution._emit,
        extra={"skipped_execution": True},
    )


def execute_locked_run(
    args: Any,
    context: Any,
    *,
    runner_cls: type[Any],
    deps: Any,
) -> int:
    execution = deps.execution
    with execution.acquire_run_lock(context.reaction_dir):
        with execution._admission_context(
            admission_root=context.admission_root,
            reaction_dir=context.reaction_dir,
            reservation_token=context.reservation_token,
            admission_app_name=context.admission_app_name,
            admission_task_id=context.admission_task_id,
        ):
            if not getattr(args, "force", False):
                existing_exit = execution._existing_completed_exit(
                    reaction_dir=context.reaction_dir,
                    selected_inp=context.selected_inp,
                    admission_root=context.admission_root,
                    reservation_token=context.reservation_token,
                    max_retries=context.max_retries,
                )
                if existing_exit is not None:
                    return existing_exit

            state, resumed = execution.load_or_create_state(
                context.reaction_dir,
                context.selected_inp,
                max_retries=context.max_retries,
                to_resolved_local=execution._to_resolved_local,
            )
            if context.admission_task_id and state.get("job_id") != context.admission_task_id:
                state["job_id"] = context.admission_task_id
                execution.save_state(context.reaction_dir, state)
            return execution._run_with_state(
                cfg=context.cfg,
                reaction_dir=context.reaction_dir,
                selected_inp=context.selected_inp,
                runner_cls=runner_cls,
                max_retries=context.max_retries,
                resumed=resumed,
                state=state,
            )


def cmd_run_inp_execute(
    args: Any,
    *,
    runner_cls: type[Any],
    cfg: Any | None,
    reaction_dir: Path | None,
    selected_inp: Path | None,
    reservation_token: str | None,
    admission_app_name: str | None,
    admission_task_id: str | None,
    deps: Any,
    logger: logging.Logger,
) -> int:
    execution = deps.execution
    statuses = deps.statuses
    context = execution._resolve_execution_context(
        args,
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        reservation_token=reservation_token,
        admission_app_name=admission_app_name,
        admission_task_id=admission_task_id,
    )
    if context is None:
        return 1

    logger.info("Selected input: %s", context.selected_inp)

    try:
        execution._recover_crashed_state(context.reaction_dir)
        return execution._execute_locked_run(args, context, runner_cls=runner_cls)
    except statuses.AdmissionLimitReachedError as exc:
        execution._release_reservation_if_needed(context.admission_root, context.reservation_token)
        logger.error("%s", exc)
        return 1
    except RuntimeError as exc:
        execution._release_reservation_if_needed(context.admission_root, context.reservation_token)
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        execution._release_reservation_if_needed(context.admission_root, context.reservation_token)
        logger.exception("Unexpected error while running input: %s", exc)
        return 1
