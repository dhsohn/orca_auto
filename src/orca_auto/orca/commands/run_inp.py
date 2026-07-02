from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    AdmissionLimitReachedError,
    release_slot,
)
from orca_auto.core.admission import (
    activate_reserved_slot as _activate_reserved_slot,
)

from .. import queue_adapter as _queue_adapter
from ..attempt_engine import _exit_with_result, run_attempts
from ..config import load_config
from ..inp_rewriter import ensure_submission_resource_request, read_resource_request_from_input
from ..orca_runner import OrcaRunner
from ..runtime.run_lock import acquire_run_lock
from ..state import save_state
from ..state_machine import load_or_create_state
from ..statuses import AnalyzerStatus, RunStatus
from ..telegram_notifier import (
    notify_queue_enqueued_event,
    notify_retry_event,
    notify_run_finished_event,
    notify_run_started_event,
)
from ..types import (
    RetryNotification,
    RunFinishedNotification,
    RunStartedNotification,
)
from . import run_inp_context as _run_inp_context
from . import run_inp_execution as _run_inp_execution
from . import run_inp_submission as _run_inp_submission
from ._helpers import (
    _emit,
    _to_resolved_local,
)
from .run_inp_context import (
    RunExecutionContext,
    RunSubmissionContext,
)
from .run_inp_deps import (
    RunInpDeps as _RunInpDeps,
)
from .run_inp_deps import (
    RunInpExecutionDeps as _RunInpExecutionDeps,
)
from .run_inp_deps import (
    RunInpNotificationDeps as _RunInpNotificationDeps,
)
from .run_inp_deps import (
    RunInpStatusDeps as _RunInpStatusDeps,
)
from .run_inp_deps import (
    RunInpSubmissionDeps as _RunInpSubmissionDeps,
)

logger = logging.getLogger(__name__)


DirectQueueSubmission = _run_inp_submission.DirectQueueSubmission
_select_latest_inp = _run_inp_execution.select_latest_inp
_retry_inp_path = _run_inp_execution.retry_inp_path
_existing_completed_out = _run_inp_execution.existing_completed_out


def _run_inp_deps() -> _RunInpDeps:
    return _RunInpDeps(
        statuses=_RunInpStatusDeps(
            AdmissionLimitReachedError=AdmissionLimitReachedError,
            AnalyzerStatus=AnalyzerStatus,
            RunStatus=RunStatus,
        ),
        execution=_RunInpExecutionDeps(
            acquire_run_lock=acquire_run_lock,
            load_or_create_state=load_or_create_state,
            release_slot=release_slot,
            run_attempts=run_attempts,
            save_state=save_state,
            _admission_context=_admission_context,
            _emit=_emit,
            _execute_locked_run=_execute_locked_run,
            _existing_completed_exit=_existing_completed_exit,
            _existing_completed_out=_existing_completed_out,
            _exit_with_result=_exit_with_result,
            _notification_callbacks=_notification_callbacks,
            _recover_crashed_state=_recover_crashed_state,
            _release_reservation_if_needed=_release_reservation_if_needed,
            _resolve_execution_context=_resolve_execution_context,
            _retry_inp_path=_retry_inp_path,
            _run_with_state=_run_with_state,
            _to_resolved_local=_to_resolved_local,
        ),
        notifications=_RunInpNotificationDeps(
            notify_queue_enqueued_event=notify_queue_enqueued_event,
            notify_retry_event=notify_retry_event,
            notify_run_finished_event=notify_run_finished_event,
            notify_run_started_event=notify_run_started_event,
        ),
        submission=_RunInpSubmissionDeps(
            ensure_submission_resource_request=ensure_submission_resource_request,
            read_resource_request_from_input=read_resource_request_from_input,
            active_direct_run_error=_active_direct_run_error,
            emit_queued_submission=_emit_queued_submission,
            queue_adapter=_queue_adapter,
            resolve_submission_context=_resolve_submission_context,
            select_latest_inp=_select_latest_inp,
            submit_reaction_dir_to_queue=submit_reaction_dir_to_queue,
        ),
    )


def _recover_crashed_state(reaction_dir: Path) -> bool:
    return _run_inp_execution.recover_crashed_state(reaction_dir, logger=logger)


def _active_direct_run_error(reaction_dir: Path) -> str | None:
    return _run_inp_execution.active_direct_run_error(reaction_dir, logger=logger)


def _emit_queued_submission(
    reaction_dir: Path,
    entry: Any,
    *,
    worker_status: str | None,
    worker_pid: int | None,
    worker_log: str | Path | None,
    worker_detail: str | None = None,
) -> None:
    _run_inp_submission.emit_queued_submission(
        reaction_dir,
        entry,
        worker_status=worker_status,
        worker_pid=worker_pid,
        worker_log=worker_log,
        worker_detail=worker_detail,
        deps=_run_inp_deps(),
    )


def _existing_completed_exit(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    admission_root: Path,
    reservation_token: str | None,
    max_retries: int,
) -> int | None:
    return _run_inp_execution.existing_completed_exit(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        admission_root=admission_root,
        reservation_token=reservation_token,
        max_retries=max_retries,
        deps=_run_inp_deps(),
    )


def _resolve_submission_context(
    args: Any,
    *,
    cfg: Any | None = None,
) -> RunSubmissionContext | None:
    return _run_inp_context.resolve_submission_context(
        args,
        cfg=cfg,
        load_config_fn=load_config,
        select_latest_inp_fn=_select_latest_inp,
        logger=logger,
    )


def _resolve_execution_context(
    args: Any,
    *,
    cfg: Any | None = None,
    reaction_dir: Path | None = None,
    selected_inp: Path | None = None,
    reservation_token: str | None = None,
    admission_app_name: str | None = None,
    admission_task_id: str | None = None,
) -> RunExecutionContext | None:
    return _run_inp_context.resolve_execution_context(
        args,
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        reservation_token=reservation_token,
        admission_app_name=admission_app_name,
        admission_task_id=admission_task_id,
        load_config_fn=load_config,
        select_latest_inp_fn=_select_latest_inp,
        logger=logger,
    )


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


def _release_reservation_if_needed(admission_root: Path, reservation_token: str | None) -> None:
    if reservation_token is not None:
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

    try:
        yield reservation_token
    finally:
        release_slot(admission_root, reservation_token)


def _notification_callbacks(
    cfg: Any,
) -> tuple[
    Callable[[RunStartedNotification], None] | None,
    Callable[[RunFinishedNotification], None] | None,
    Callable[[RetryNotification], None] | None,
]:
    return _run_inp_execution.notification_callbacks(cfg, deps=_run_inp_deps())


def _run_with_state(
    *,
    cfg: Any,
    reaction_dir: Path,
    selected_inp: Path,
    runner_cls: type[OrcaRunner],
    max_retries: int,
    resumed: bool,
    state: Any,
) -> int:
    return _run_inp_execution.run_with_state(
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        runner_cls=runner_cls,
        max_retries=max_retries,
        resumed=resumed,
        state=state,
        deps=_run_inp_deps(),
    )


def _execute_locked_run(
    args: Any,
    context: RunExecutionContext,
    *,
    runner_cls: type[OrcaRunner],
) -> int:
    return _run_inp_execution.execute_locked_run(
        args,
        context,
        runner_cls=runner_cls,
        deps=_run_inp_deps(),
    )


def _cmd_run_inp_execute(
    args: Any,
    *,
    runner_cls: type[OrcaRunner] = OrcaRunner,
    cfg: Any | None = None,
    reaction_dir: Path | None = None,
    selected_inp: Path | None = None,
    reservation_token: str | None = None,
    admission_app_name: str | None = None,
    admission_task_id: str | None = None,
) -> int:
    return _run_inp_execution.cmd_run_inp_execute(
        args,
        runner_cls=runner_cls,
        cfg=cfg,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        reservation_token=reservation_token,
        admission_app_name=admission_app_name,
        admission_task_id=admission_task_id,
        deps=_run_inp_deps(),
        logger=logger,
    )


def _cmd_run_inp_submit(args: Any, *, runner_cls: type[OrcaRunner] = OrcaRunner) -> int:
    return _run_inp_submission.cmd_run_inp_submit(
        args,
        runner_cls=runner_cls,
        deps=_run_inp_deps(),
        logger=logger,
    )


def submit_reaction_dir_to_queue(args: Any) -> DirectQueueSubmission:
    return _run_inp_submission.submit_reaction_dir_to_queue(args, deps=_run_inp_deps())


def cmd_run_inp(args: Any, *, runner_cls: type[OrcaRunner] = OrcaRunner) -> int:
    return _cmd_run_inp_submit(args, runner_cls=runner_cls)
