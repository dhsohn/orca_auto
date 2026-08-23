from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.commands.queue import display_status
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.statuses import STATUS_WAITING_FOR_SLOT
from orca_auto.core.utils import normalize_text as _normalize_text
from orca_auto.flow._orca_stage_materialization import validate_workflow_orca_input_bytes

from . import internal_engine_models as _engine_models

_SUBMIT_API_NAME = "orca_auto.orca.direct_submit"
_CANCEL_API_NAME = "orca_auto.orca.direct_cancel"


@dataclass(frozen=True)
class _OrcaDirectSubmitRequest:
    command_argv: list[str]
    args: Namespace
    reaction_dir: str
    priority: int
    force: bool


@dataclass(frozen=True)
class _OrcaDirectCancelRequest:
    command_argv: list[str]
    config_path: str
    target: str


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
    expected_selected_inp: str | None,
    workflow_task_kind: str | None,
) -> _OrcaDirectSubmitRequest:
    normalized_config = _normalize_text(config_path)
    priority_value = normalize_queue_priority(priority)
    force_value = bool(force)
    trace_kwargs: dict[str, Any] = {
        "reaction_dir": reaction_dir,
        "priority": priority_value,
        "force": force_value,
    }
    workflow_validator = _workflow_bound_payload_validator(
        expected_selected_inp=expected_selected_inp,
        workflow_task_kind=workflow_task_kind,
    )

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
            expected_selected_inp=expected_selected_inp,
            workflow_task_kind=workflow_task_kind,
            bound_selected_validator=workflow_validator,
        ),
        reaction_dir=reaction_dir,
        priority=priority_value,
        force=force_value,
    )


def _workflow_bound_payload_validator(
    *,
    expected_selected_inp: str | None,
    workflow_task_kind: str | None,
) -> Callable[[Path, bytes], None] | None:
    if expected_selected_inp is None and workflow_task_kind is None:
        return None
    selected_text = (expected_selected_inp or "").strip()
    task_kind = (workflow_task_kind or "").strip()
    if not selected_text or not task_kind:
        raise ValueError(
            "Workflow ORCA direct submission requires task kind and selected input together"
        )

    def validate_bound_payload(bound_inp: Path, payload: bytes) -> None:
        validate_workflow_orca_input_bytes(
            task_kind=task_kind,
            inp_path=bound_inp,
            input_bytes=payload,
        )

    return validate_bound_payload


def _submit_reaction_dir_to_queue(args: Namespace) -> Any:
    from orca_auto.orca import submission

    return submission.submit_reaction_dir_to_queue(args)


def _submission_reaction_dir(submission: Any, default_reaction_dir: str) -> str:
    context = submission.context
    return str(context.reaction_dir) if context is not None else default_reaction_dir


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
    expected_selected_inp: str | None = None,
    workflow_task_kind: str | None = None,
) -> dict[str, Any]:
    del repo_root
    request = _submit_request(
        reaction_dir=reaction_dir,
        priority=priority,
        config_path=config_path,
        max_cores=max_cores,
        max_memory_gb=max_memory_gb,
        force=force,
        expected_selected_inp=expected_selected_inp,
        workflow_task_kind=workflow_task_kind,
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


__all__ = [
    "cancel_target",
    "submit_reaction_dir",
]
