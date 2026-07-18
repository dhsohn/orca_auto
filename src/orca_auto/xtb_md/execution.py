from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from orca_auto.core.admission import get_slot
from orca_auto.core.config.engines import load_xtb_md_config
from orca_auto.core.engines import (
    entry_matches_engine_identity,
    own_engine_accept_entry,
)
from orca_auto.core.queue import (
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    update_metadata,
)
from orca_auto.core.queue.child.execution import ChildWorkerShutdownController
from orca_auto.core.queue.lifecycle import entry_status_is_running
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
)
from orca_auto.core.statuses import TERMINAL_STATUSES

from .records import (
    build_job_artifact,
    execution_snapshot,
    persist_failed_job,
    persist_job_artifact,
)
from .runner import XtbMdRunResult, run_xtb_md_attempt

_ACCEPT_ENTRY = own_engine_accept_entry("xtb_md")
_HANDOFF_TIMEOUT_SECONDS = 10.0


def _find_entry(queue_root: Path, queue_id: str) -> Any | None:
    matches = [
        entry
        for entry in list_queue(queue_root)
        if str(getattr(entry, "queue_id", "") or "") == queue_id
        and entry_matches_engine_identity(entry, "xtb_md")
    ]
    return matches[0] if len(matches) == 1 else None


def _await_parent_handoff(
    admission_root: str | Path,
    admission_token: str,
    *,
    timeout_seconds: float = _HANDOFF_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        slot = get_slot(admission_root, admission_token)
        if slot is None:
            return False
        if slot.owner_pid == os.getpid():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _running_record_callback(
    cfg: Any,
    queue_root: Path,
    entry_ref: list[Any],
) -> Any:
    def on_started(
        execution_dir: str,
        started_at: str,
        command: tuple[str, ...],
    ) -> None:
        expected_entry = entry_ref[0]
        updated = update_metadata(
            queue_root,
            expected_entry.queue_id,
            {
                "execution_dir": execution_dir,
                "attempt": 1,
                "retry_supported": False,
                "resume_supported": False,
            },
            accept_entry_fn=_ACCEPT_ENTRY,
            expected_entry=expected_entry,
            expected_task_id=expected_entry.task_id,
        )
        if updated is None:
            raise RuntimeError("xTB-MD queue generation changed before process attachment")
        entry_ref[0] = updated
        payload = build_job_artifact(
            updated,
            status="running",
            reason="running",
            exit_code=None,
            artifacts={"execution_dir": execution_dir},
            engine_payload={"command": list(command)},
            worker_pid=os.getpid(),
            started_at=started_at,
        )
        persist_job_artifact(cfg, updated, payload)

    return on_started


def _status_text(entry: Any) -> str:
    raw = getattr(entry, "status", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _cancelled_result(result: XtbMdRunResult) -> XtbMdRunResult:
    return XtbMdRunResult(
        status="cancelled",
        reason="cancel_requested",
        exit_code=result.exit_code,
        execution_dir=result.execution_dir,
        started_at=result.started_at,
        finished_at=result.finished_at,
        command=result.command,
        artifacts=result.artifacts,
        engine_payload=result.engine_payload,
    )


def _persist_terminal_result(
    cfg: Any,
    queue_root: Path,
    entry: Any,
    result: XtbMdRunResult,
) -> XtbMdRunResult:
    artifacts = {
        "execution_dir": result.execution_dir,
        "terminal_outputs": result.artifacts,
    }
    engine_payload = {
        **result.engine_payload,
        "command": list(result.command),
    }
    payload = build_job_artifact(
        entry,
        status=result.status,
        reason=result.reason,
        exit_code=result.exit_code,
        artifacts=artifacts,
        engine_payload=engine_payload,
        worker_pid=os.getpid(),
        started_at=result.started_at or None,
        finished_at=result.finished_at or None,
    )
    persist_job_artifact(cfg, entry, payload)
    generation_kwargs = {
        "accept_entry_fn": _ACCEPT_ENTRY,
        "expected_entry": entry,
        "expected_task_id": entry.task_id,
        "metadata_update": {
            "execution_dir": result.execution_dir,
            "attempt": 1,
            "retry_supported": False,
            "resume_supported": False,
            "terminal_artifacts": result.artifacts,
        },
    }
    if result.status == "completed":
        updated = mark_completed(queue_root, entry.queue_id, **generation_kwargs)
    elif result.status == "cancelled":
        updated = mark_cancelled(
            queue_root,
            entry.queue_id,
            error=result.reason,
            require_cancel_requested=True,
            **generation_kwargs,
        )
    else:
        updated = mark_failed(
            queue_root,
            entry.queue_id,
            error=result.reason,
            **generation_kwargs,
        )
    if updated is not None:
        return result

    current = _find_entry(queue_root, entry.queue_id)
    if current is not None and bool(getattr(current, "cancel_requested", False)):
        cancelled = _cancelled_result(result)
        cancelled_payload = build_job_artifact(
            current,
            status="cancelled",
            reason=cancelled.reason,
            exit_code=cancelled.exit_code,
            artifacts=artifacts,
            engine_payload=engine_payload,
            worker_pid=os.getpid(),
            started_at=cancelled.started_at or None,
            finished_at=cancelled.finished_at or None,
        )
        persist_job_artifact(cfg, current, cancelled_payload)
        marked = mark_cancelled(
            queue_root,
            current.queue_id,
            error=cancelled.reason,
            metadata_update=generation_kwargs["metadata_update"],
            accept_entry_fn=_ACCEPT_ENTRY,
            expected_entry=current,
            expected_task_id=current.task_id,
            require_cancel_requested=True,
        )
        if marked is not None:
            return cancelled
    if current is not None and _status_text(current) == result.status:
        return result
    raise RuntimeError("xTB-MD terminal queue generation changed before finalization")


def _fail_unfinished_entry(
    cfg: Any,
    queue_root: Path,
    entry: Any,
    *,
    reason: str,
) -> None:
    try:
        persist_failed_job(cfg, entry, reason=reason)
    except Exception:  # noqa: BLE001
        pass
    mark_failed(
        queue_root,
        entry.queue_id,
        error=reason,
        accept_entry_fn=_ACCEPT_ENTRY,
        expected_entry=entry,
        expected_task_id=entry.task_id,
    )


def run_worker_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    **_unused: Any,
) -> int:
    cfg = load_xtb_md_config(config_path)
    resolved_queue_root = Path(queue_root).expanduser().resolve()
    entry = _find_entry(resolved_queue_root, queue_id)
    if entry is None or not entry_status_is_running(entry):
        return 1
    if not admission_token:
        _fail_unfinished_entry(
            cfg,
            resolved_queue_root,
            entry,
            reason="admission_token_missing",
        )
        return 1
    admission_root = resolve_admission_root(cfg)
    if not _await_parent_handoff(admission_root, admission_token):
        return 1

    controller = ChildWorkerShutdownController()
    install_shutdown_signal_handlers(controller.request)
    active_entry = [entry]
    try:
        snapshot = execution_snapshot(entry)
        result = run_xtb_md_attempt(
            cfg,
            entry,
            execution_snapshot=snapshot,
            admission_root=admission_root,
            admission_token=admission_token,
            should_cancel=lambda: get_cancel_requested(
                resolved_queue_root,
                active_entry[0].queue_id,
                accept_entry_fn=_ACCEPT_ENTRY,
                expected_entry=active_entry[0],
                expected_task_id=active_entry[0].task_id,
            ),
            shutdown_requested=controller.is_requested,
            on_started=_running_record_callback(cfg, resolved_queue_root, active_entry),
        )
        terminal = _persist_terminal_result(
            cfg,
            resolved_queue_root,
            active_entry[0],
            result,
        )
    except Exception as exc:  # noqa: BLE001
        reason = f"worker_execution_error:{exc.__class__.__name__}:{exc}"[:1000]
        current = _find_entry(resolved_queue_root, entry.queue_id)
        if current is not None and _status_text(current) not in TERMINAL_STATUSES:
            _fail_unfinished_entry(
                cfg,
                resolved_queue_root,
                current,
                reason=reason,
            )
        return 1
    return 0 if terminal.status in {"completed", "cancelled"} else 1


__all__ = ["run_worker_job"]
