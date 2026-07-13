from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.commands.queue import display_status
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.publication import (
    QUEUE_SUBMISSION_INTENT_KEY,
    queue_entry_is_claimable,
)
from orca_auto.core.statuses import STATUS_WAITING_FOR_SLOT
from orca_auto.core.utils import normalize_text as _normalize_text
from orca_auto.core.utils import now_utc_iso

from ..registry import sync_workflow_registry
from ..state import (
    acquire_workflow_lock,
    load_workflow_payload,
    resolve_workflow_workspace,
    write_workflow_payload,
)
from . import internal_engine_models as _engine_models
from . import orca_cancellation as _cancellation
from . import orca_submission as _submission

_SUBMIT_API_NAME = "orca_auto.orca.direct_submit"
_CANCEL_API_NAME = "orca_auto.orca.direct_cancel"


@dataclass(frozen=True)
class _OrcaDirectSubmitRequest:
    command_argv: list[str]
    args: Namespace
    reaction_dir: str
    priority: int
    force: bool
    submission_intent_token: str


@dataclass(frozen=True)
class _OrcaDirectCancelRequest:
    command_argv: list[str]
    config_path: str
    target: str


@dataclass(frozen=True)
class _RecoveredQueuedSubmission:
    entry: Any
    reaction_dir: Path
    worker_info: Any


def _trace_argv(*, api_name: str, config_path: str, kwargs: dict[str, Any]) -> list[str]:
    return _engine_models.internal_call_argv(
        api_name=api_name,
        config_path=config_path,
        kwargs=kwargs,
    )


def _key_value_stdout(fields: dict[str, Any]) -> str:
    return _engine_models._key_value_stdout(_engine_models._text_fields(fields))


def _failure_payload(
    *,
    command_argv: list[str],
    stderr: str,
    reaction_dir: str = "",
    reason: str = "",
) -> dict[str, Any]:
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return _engine_models.InternalEngineCommandResult(
        status="failed",
        reason=reason,
        returncode=1,
        command_argv=command_argv,
        stderr=stderr,
        extra_fields={
            "reaction_dir": reaction_dir,
            "priority": 0,
            "force": False,
        },
    ).to_payload()


def _deferred_payload(
    *,
    command_argv: list[str],
    stderr: str,
    reaction_dir: str,
    reason: str,
) -> dict[str, Any]:
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return _engine_models.InternalEngineCommandResult(
        status=STATUS_WAITING_FOR_SLOT,
        reason=reason,
        returncode=0,
        command_argv=command_argv,
        stderr=stderr,
        extra_fields={
            "reaction_dir": reaction_dir,
            "priority": 0,
            "force": False,
        },
    ).to_payload()


def _queued_payload(
    *,
    command_argv: list[str],
    result: Any,
    priority: int,
    force: bool,
) -> dict[str, Any]:
    from orca_auto.orca.queue import adapter as queue_adapter

    entry = result.entry
    parsed = {
        "status": "queued",
        "job_dir": result.reaction_dir,
        "queue_id": queue_adapter.queue_entry_id(entry),
        "job_id": queue_adapter.queue_entry_task_id(entry),
        "priority": priority,
    }
    if force:
        parsed["force"] = "true"
    if result.worker_info.status:
        parsed["worker"] = result.worker_info.status
    if result.worker_info.pid is not None:
        parsed["worker_pid"] = result.worker_info.pid
    if result.worker_info.log_file:
        parsed["worker_log"] = result.worker_info.log_file
    if result.worker_info.detail:
        parsed["worker_detail"] = result.worker_info.detail
    parsed_stdout = _engine_models._text_fields(parsed)
    return _engine_models.InternalEngineCommandResult(
        status="submitted",
        reason="",
        returncode=0,
        command_argv=command_argv,
        stdout=_key_value_stdout(parsed_stdout),
        parsed_stdout=parsed_stdout,
        queue_id=parsed_stdout.get("queue_id", ""),
        job_id=parsed_stdout.get("job_id", ""),
        extra_fields={
            "reaction_dir": parsed_stdout.get("job_dir", _normalize_text(result.reaction_dir)),
            "priority": priority,
            "force": force,
        },
    ).to_payload()


def _cancel_request(*, target: str, config_path: str) -> _OrcaDirectCancelRequest:
    normalized_config = _normalize_text(config_path)
    normalized_target = _normalize_text(target)
    return _OrcaDirectCancelRequest(
        command_argv=_trace_argv(
            api_name=_CANCEL_API_NAME,
            config_path=normalized_config,
            kwargs={"target": normalized_target},
        ),
        config_path=normalized_config,
        target=normalized_target,
    )


def _find_orca_cancel_entry(request: _OrcaDirectCancelRequest) -> tuple[Path, Any] | None:
    from orca_auto.orca.config import load_config
    from orca_auto.orca.queue import adapter as queue_adapter

    cfg = load_config(request.config_path)
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    matched = queue_adapter.find_entry_by_target(
        queue_adapter.list_queue(allowed_root),
        request.target,
    )
    if matched is None:
        return None
    return allowed_root, matched


def _request_orca_cancel(allowed_root: Path, entry: Any) -> Any | None:
    from orca_auto.orca.queue import adapter as queue_adapter

    return queue_adapter.cancel(
        allowed_root,
        queue_adapter.queue_entry_id(entry),
        expected_entry=entry,
    )


def _cancel_request_targets_exact_entry(
    request: _OrcaDirectCancelRequest,
    entry: Any,
) -> bool:
    from orca_auto.orca.queue import adapter as queue_adapter

    return bool(
        queue_adapter.is_orca_queue_entry(entry)
        and queue_adapter.queue_entry_matches_target(entry, request.target)
    )


def _cancel_success_payload(
    *,
    command_argv: list[str],
    updated: Any,
) -> dict[str, Any]:
    from orca_auto.orca.queue import adapter as queue_adapter

    status = display_status(updated)
    parsed_stdout = _engine_models._text_fields(
        {
            "status": status,
            "queue_id": queue_adapter.queue_entry_id(updated),
            "job_id": queue_adapter.queue_entry_task_id(updated),
        }
    )
    return _engine_models.InternalEngineCommandResult(
        status=status,
        reason="",
        returncode=0,
        command_argv=command_argv,
        stdout=_key_value_stdout(parsed_stdout),
        parsed_stdout=parsed_stdout,
        queue_id=parsed_stdout.get("queue_id", ""),
        job_id=parsed_stdout.get("job_id", ""),
    ).to_payload()


def _submit_request(
    *,
    reaction_dir: str,
    priority: int,
    config_path: str,
    max_cores: int | None,
    max_memory_gb: int | None,
    force: bool,
    submission_intent_token: str = "",
) -> _OrcaDirectSubmitRequest:
    normalized_config = _normalize_text(config_path)
    priority_value = normalize_queue_priority(priority)
    force_value = bool(force)
    intent_token = _normalize_text(submission_intent_token)
    trace_kwargs: dict[str, Any] = {
        "reaction_dir": reaction_dir,
        "priority": priority_value,
        "force": force_value,
    }
    if intent_token:
        trace_kwargs[QUEUE_SUBMISSION_INTENT_KEY] = intent_token
    return _OrcaDirectSubmitRequest(
        command_argv=_trace_argv(
            api_name=_SUBMIT_API_NAME,
            config_path=normalized_config,
            kwargs=trace_kwargs,
        ),
        args=Namespace(
            config=normalized_config,
            path=reaction_dir,
            priority=priority_value,
            force=force_value,
            max_cores=max_cores,
            max_memory_gb=max_memory_gb,
            submission_intent_token=intent_token,
        ),
        reaction_dir=reaction_dir,
        priority=priority_value,
        force=force_value,
        submission_intent_token=intent_token,
    )


def _submit_reaction_dir_to_queue(args: Namespace) -> Any:
    from orca_auto.orca.commands import run_inp as _run_inp

    return _run_inp.submit_reaction_dir_to_queue(args)


def _submission_reaction_dir(submission: Any, default_reaction_dir: str) -> str:
    context = submission.context
    return str(context.reaction_dir) if context is not None else default_reaction_dir


def _entry_matches_submission_request(
    entry: Any,
    *,
    reaction_dir: Path,
    priority: int,
    force: bool,
    submission_intent_token: str,
    allow_unclaimable_active: bool = False,
) -> bool:
    from orca_auto.orca.queue import adapter as queue_adapter

    status = queue_adapter.queue_entry_status(entry)
    if status in queue_adapter.ACTIVE_STATUSES:
        eligible_status = not bool(getattr(entry, "cancel_requested", False)) and (
            allow_unclaimable_active or bool(queue_entry_is_claimable(entry))
        )
    else:
        eligible_status = status in queue_adapter.TERMINAL_STATUSES
    return bool(
        eligible_status
        and queue_adapter.queue_entry_app_name(entry) == queue_adapter.QUEUE_APP_NAME
        and _normalize_text(getattr(entry, "task_kind", "")) == queue_adapter.QUEUE_TASK_KIND
        and _normalize_text(getattr(entry, "engine", "")) == queue_adapter.QUEUE_ENGINE
        and queue_adapter.queue_entry_reaction_dir(entry) == str(reaction_dir)
        and queue_adapter.queue_entry_priority(entry) == priority
        and queue_adapter.queue_entry_force(entry) is force
        and _normalize_text(
            queue_adapter.queue_entry_metadata(entry).get(QUEUE_SUBMISSION_INTENT_KEY)
        )
        == submission_intent_token
    )


def recover_exact_reaction_dir_submission(
    *,
    reaction_dir: str,
    priority: int,
    config_path: str,
    max_cores: int | None = None,
    max_memory_gb: int | None = None,
    force: bool = False,
    repo_root: str | None = None,
    submission_intent_token: str = "",
    allow_unclaimable_active: bool = False,
    raise_on_lookup_error: bool = False,
) -> dict[str, Any] | None:
    """Adopt the exact ORCA row created by a replayed workflow submission."""
    del max_cores, max_memory_gb, repo_root
    from orca_auto.orca.commands.run_inp_context import WorkerStatusInfo
    from orca_auto.orca.config import load_config
    from orca_auto.orca.queue import adapter as queue_adapter

    request = _submit_request(
        reaction_dir=reaction_dir,
        priority=priority,
        config_path=config_path,
        max_cores=None,
        max_memory_gb=None,
        force=force,
        submission_intent_token=submission_intent_token,
    )
    if not request.submission_intent_token:
        return None
    try:
        cfg = load_config(request.args.config)
        allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
        resolved_reaction_dir = Path(reaction_dir).expanduser().resolve()
        entries = queue_adapter.list_queue(allowed_root)
    except (OSError, ValueError):
        if raise_on_lookup_error:
            raise
        return None
    matches = [
        entry
        for entry in entries
        if _entry_matches_submission_request(
            entry,
            reaction_dir=resolved_reaction_dir,
            priority=request.priority,
            force=request.force,
            submission_intent_token=request.submission_intent_token,
            allow_unclaimable_active=allow_unclaimable_active,
        )
    ]
    if not matches:
        return None
    if len(matches) > 1:
        return _deferred_payload(
            command_argv=request.command_argv,
            reaction_dir=str(resolved_reaction_dir),
            stderr=(
                "multiple queue entries match the workflow submission intent; "
                "refusing to choose an ambiguous queue generation"
            ),
            reason="ambiguous_submission_recovery",
        )
    entry = matches[0]
    entry_status = queue_adapter.queue_entry_status(entry)
    return _queued_payload(
        command_argv=request.command_argv,
        result=_RecoveredQueuedSubmission(
            entry=entry,
            reaction_dir=resolved_reaction_dir,
            worker_info=WorkerStatusInfo(
                detail=(
                    f"recovered exact {entry_status} queue entry after workflow submission replay"
                )
            ),
        ),
        priority=request.priority,
        force=request.force,
    )


def _failure_payload_for_submission(
    *,
    request: _OrcaDirectSubmitRequest,
    submission: Any,
) -> dict[str, Any] | None:
    if submission.reason == "invalid_submission_target":
        return _failure_payload(
            command_argv=request.command_argv,
            reaction_dir=request.reaction_dir,
            stderr=submission.stderr,
            reason="invalid_submission_target",
        )
    if submission.reason == "submission_conflict":
        return _deferred_payload(
            command_argv=request.command_argv,
            reaction_dir=_submission_reaction_dir(submission, request.reaction_dir),
            stderr=submission.stderr,
            reason="submission_conflict",
        )
    if submission.status != "submitted" or submission.queued_result is None:
        return _failure_payload(
            command_argv=request.command_argv,
            reaction_dir=_submission_reaction_dir(submission, request.reaction_dir),
            stderr=submission.stderr or "failed to submit ORCA queue entry",
            reason=submission.reason or "submission_failed",
        )
    return None


def submit_reaction_dir(
    *,
    reaction_dir: str,
    priority: int,
    config_path: str,
    max_cores: int | None = None,
    max_memory_gb: int | None = None,
    force: bool = False,
    repo_root: str | None = None,
    submission_intent_token: str = "",
) -> dict[str, Any]:
    del repo_root
    request = _submit_request(
        reaction_dir=reaction_dir,
        priority=priority,
        config_path=config_path,
        max_cores=max_cores,
        max_memory_gb=max_memory_gb,
        force=force,
        submission_intent_token=submission_intent_token,
    )
    try:
        submission = _submit_reaction_dir_to_queue(request.args)
        failure_payload = _failure_payload_for_submission(request=request, submission=submission)
        if failure_payload is not None:
            return failure_payload
        queued = submission.queued_result
    except Exception as exc:  # noqa: BLE001
        return _failure_payload(
            command_argv=request.command_argv,
            reaction_dir=reaction_dir,
            stderr=f"{exc.__class__.__name__}: {exc}",
            reason="submission_failed",
        )
    return _queued_payload(
        command_argv=request.command_argv,
        result=queued,
        priority=request.priority,
        force=request.force,
    )


def cancel_target(
    *,
    target: str,
    config_path: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    del repo_root
    request = _cancel_request(target=target, config_path=config_path)
    if not request.target:
        return _failure_payload(
            command_argv=request.command_argv,
            stderr="queue cancel requires a target",
        )

    allowed_root: Path | None = None
    matched: Any | None = None
    try:
        entry_with_root = _find_orca_cancel_entry(request)
        if entry_with_root is None:
            return _failure_payload(
                command_argv=request.command_argv,
                stderr=f"queue target not found: {request.target}",
                reason="target_not_found",
            )
        allowed_root, matched = entry_with_root
        updated = _request_orca_cancel(allowed_root, matched)
        if updated is None:
            from orca_auto.orca.queue import adapter as queue_adapter

            current = queue_adapter.get_entry_by_id(
                allowed_root,
                queue_adapter.queue_entry_id(matched),
            )
            if (
                current is None
                or not _cancel_request_targets_exact_entry(request, current)
                or not queue_adapter.queue_entries_same_publication_generation(current, matched)
                or queue_adapter.queue_entry_status(current) != "cancelled"
            ):
                return _failure_payload(
                    command_argv=request.command_argv,
                    stderr=f"queue target already terminal: {request.target}",
                    reason="already_terminal",
                )
            updated = current
    except Exception as exc:  # noqa: BLE001
        if allowed_root is not None and matched is not None:
            try:
                from orca_auto.orca.queue import adapter as queue_adapter

                current = queue_adapter.get_entry_by_id(
                    allowed_root,
                    queue_adapter.queue_entry_id(matched),
                )
                committed = bool(
                    current is not None
                    and queue_adapter.queue_entries_same_publication_generation(current, matched)
                    and (
                        queue_adapter.queue_entry_status(current) == "cancelled"
                        or (
                            queue_adapter.queue_entry_status(current)
                            in queue_adapter.ACTIVE_STATUSES
                            and bool(getattr(current, "cancel_requested", False))
                        )
                    )
                )
                if committed:
                    return _cancel_success_payload(
                        command_argv=request.command_argv,
                        updated=current,
                    )
            except Exception:  # noqa: BLE001
                pass
        return _failure_payload(
            command_argv=request.command_argv,
            stderr=f"{exc.__class__.__name__}: {exc}",
            reason="cancel_failed",
        )

    return _cancel_success_payload(command_argv=request.command_argv, updated=updated)


def _submission_deps() -> _submission.SubmissionDeps:
    return _submission.SubmissionDeps(
        normalize_text=_normalize_text,
        now_utc_iso=now_utc_iso,
        resolve_workflow_workspace=resolve_workflow_workspace,
        acquire_workflow_lock=acquire_workflow_lock,
        load_workflow_payload=load_workflow_payload,
        write_workflow_payload=write_workflow_payload,
        sync_workflow_registry=sync_workflow_registry,
        submit_reaction_dir=submit_reaction_dir,
        recover_exact_reaction_dir_submission=recover_exact_reaction_dir_submission,
    )


def submit_reaction_ts_search_workflow(
    *,
    workflow_target: str,
    workflow_root: str | Path | None,
    orca_config: str,
    orca_repo_root: str | None = None,
    skip_submitted: bool = True,
) -> dict[str, Any]:
    return _submission.submit_reaction_ts_search_workflow(
        workflow_target=workflow_target,
        workflow_root=workflow_root,
        orca_config=orca_config,
        orca_repo_root=orca_repo_root,
        skip_submitted=skip_submitted,
        deps=_submission_deps(),
    )


def _cancellation_deps() -> _cancellation.CancellationDeps:
    return _cancellation.CancellationDeps(
        normalize_text=_normalize_text,
        now_utc_iso=now_utc_iso,
        resolve_workflow_workspace=resolve_workflow_workspace,
        acquire_workflow_lock=acquire_workflow_lock,
        load_workflow_payload=load_workflow_payload,
        write_workflow_payload=write_workflow_payload,
        sync_workflow_registry=sync_workflow_registry,
        cancel_target=cancel_target,
        recover_exact_reaction_dir_submission=recover_exact_reaction_dir_submission,
    )


def cancel_reaction_ts_search_workflow(
    *,
    workflow_target: str,
    workflow_root: str | Path | None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
) -> dict[str, Any]:
    return _cancellation.cancel_reaction_ts_search_workflow(
        workflow_target=workflow_target,
        workflow_root=workflow_root,
        orca_config=orca_config,
        orca_repo_root=orca_repo_root,
        deps=_cancellation_deps(),
    )


__all__ = [
    "cancel_target",
    "cancel_reaction_ts_search_workflow",
    "submit_reaction_dir",
    "submit_reaction_ts_search_workflow",
]
