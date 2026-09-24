from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.queue.types import QueueStatus, effective_queue_status
from orca_auto.core.statuses import STATUS_FAILED
from orca_auto.core.utils import normalize_text as _normalize_text

from .engine_runtime import engine_runtime_paths
from .queue import adapter as queue_adapter
from .queue.entries import queue_entry_is_retired_workflow_owned

_CANCEL_API_NAME = "orca_auto.orca.direct_cancel"


@dataclass(frozen=True)
class InternalEngineCommandResult:
    """JSON-ready outcome of one internal engine command call."""

    status: str
    command_argv: list[str]
    returncode: int
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    parsed_stdout: dict[str, str] = field(default_factory=dict)
    job_id: str = ""
    queue_id: str = ""
    job_dir: str = ""
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "returncode": self.returncode,
            "command_argv": list(self.command_argv),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "parsed_stdout": dict(self.parsed_stdout),
            "job_id": self.job_id,
            "queue_id": self.queue_id,
        }
        if self.job_dir:
            payload["job_dir"] = self.job_dir
        payload.update(self.extra_fields)
        return payload


def internal_call_argv(
    *,
    api_name: str,
    config_path: str,
    kwargs: dict[str, Any],
) -> list[str]:
    return [
        api_name,
        f"config={config_path}",
        *[f"{key}={value}" for key, value in kwargs.items()],
    ]


def _text_fields(fields: dict[str, Any]) -> dict[str, str]:
    return {key: text for key, value in fields.items() if (text := _normalize_text(value))}


@dataclass(frozen=True)
class _OrcaDirectCancelRequest:
    command_argv: list[str]
    config_path: str
    target: str


def _trace_argv(*, api_name: str, config_path: str, kwargs: dict[str, Any]) -> list[str]:
    return internal_call_argv(
        api_name=api_name,
        config_path=config_path,
        kwargs=kwargs,
    )


def _key_value_stdout(fields: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in _text_fields(fields).items() if value)


def _failure_payload(
    *,
    command_argv: list[str],
    stderr: str,
    reaction_dir: str = "",
    reason: str = "",
) -> dict[str, Any]:
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return InternalEngineCommandResult(
        status=STATUS_FAILED,
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
    allowed_root = engine_runtime_paths(request.config_path)["allowed_root"]
    matched = queue_adapter.find_entry_by_target(
        queue_adapter.list_queue(allowed_root),
        request.target,
    )
    if matched is None:
        return None
    return allowed_root, matched


def _request_orca_cancel(allowed_root: Path, entry: Any) -> Any | None:
    return queue_adapter.cancel(
        allowed_root,
        queue_adapter.queue_entry_id(entry),
        expected_entry=entry,
    )


def _cancel_request_targets_exact_entry(
    request: _OrcaDirectCancelRequest,
    entry: Any,
) -> bool:
    return bool(
        queue_adapter.is_orca_queue_entry(entry)
        and queue_adapter.queue_entry_matches_target(entry, request.target)
    )


def _cancel_success_payload(
    *,
    command_argv: list[str],
    updated: Any,
) -> dict[str, Any]:
    status = effective_queue_status(updated)
    parsed_stdout = _text_fields(
        {
            "status": status,
            "queue_id": queue_adapter.queue_entry_id(updated),
            "job_id": queue_adapter.queue_entry_task_id(updated),
        }
    )
    return InternalEngineCommandResult(
        status=status,
        reason="",
        returncode=0,
        command_argv=command_argv,
        stdout=_key_value_stdout(parsed_stdout),
        parsed_stdout=parsed_stdout,
        queue_id=parsed_stdout.get("queue_id", ""),
        job_id=parsed_stdout.get("job_id", ""),
    ).to_payload()


def cancel_target(
    *,
    target: str,
    config_path: str,
) -> dict[str, Any]:
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
        if queue_entry_is_retired_workflow_owned(matched, allowed_root):
            return _failure_payload(
                command_argv=request.command_argv,
                stderr="Workflow directories are retired; use the previous runtime to cancel this job",
                reason="retired_workflow",
            )
        updated = _request_orca_cancel(allowed_root, matched)
        if updated is None:
            current = queue_adapter.get_entry_by_id(
                allowed_root,
                queue_adapter.queue_entry_id(matched),
            )
            if (
                current is None
                or not _cancel_request_targets_exact_entry(request, current)
                or not queue_adapter.queue_entries_same_publication_generation(current, matched)
                or queue_adapter.queue_entry_status(current) != QueueStatus.CANCELLED.value
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
                current = queue_adapter.get_entry_by_id(
                    allowed_root,
                    queue_adapter.queue_entry_id(matched),
                )
                committed = bool(
                    current is not None
                    and queue_adapter.queue_entries_same_publication_generation(current, matched)
                    and (
                        queue_adapter.queue_entry_status(current) == QueueStatus.CANCELLED.value
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
