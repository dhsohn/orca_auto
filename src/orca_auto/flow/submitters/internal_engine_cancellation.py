from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from orca_auto.core.queue.store import queue_entries_same_generation
from orca_auto.core.statuses import (
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from orca_auto.core.utils import normalize_text

from .internal_engine_models import (
    InternalEngineCommandResult,
    InternalEngineSubmitterDeps,
    InternalEngineSubmitterSpec,
    _key_value_stdout,
    _stderr_with_exception,
    _text_fields,
    internal_call_argv,
)


@dataclass(frozen=True)
class _InternalEngineCancelRequest:
    command_trace: list[str]
    target: str


@dataclass(frozen=True)
class _InternalEngineCancelMatch:
    queue_root: Any
    entry: Any


def _queue_entry_status_text(entry: Any) -> str:
    status_value = getattr(getattr(entry, "status", None), "value", None)
    return normalize_text(status_value or getattr(entry, "status", ""))


def _direct_cancel_status(entry: Any, displayed_status: str) -> str:
    normalized_display = normalize_text(displayed_status).lower()
    if normalized_display == STATUS_CANCEL_REQUESTED:
        return STATUS_CANCEL_REQUESTED
    if normalized_display == STATUS_CANCELLED:
        return STATUS_CANCELLED
    if _queue_entry_status_text(entry).lower() == STATUS_RUNNING and getattr(
        entry, "cancel_requested", False
    ):
        return STATUS_CANCEL_REQUESTED
    return STATUS_CANCELLED


def _cancel_failure_payload(
    *,
    command_trace: list[str],
    stderr: str,
) -> dict[str, Any]:
    return InternalEngineCommandResult(
        status=STATUS_FAILED,
        reason="cancel_command_failed",
        returncode=1,
        command_argv=command_trace,
        stderr=stderr,
    ).to_payload()


def _cancel_success_payload(
    *,
    command_trace: list[str],
    status: str,
    parsed: dict[str, str],
) -> dict[str, Any]:
    return InternalEngineCommandResult(
        status=status,
        returncode=0,
        command_argv=command_trace,
        stdout=_key_value_stdout(parsed),
        parsed_stdout=parsed,
        queue_id=parsed.get("queue_id", ""),
        job_id=parsed.get("job_id", ""),
    ).to_payload()


def _cancel_request(
    *,
    api_name: str,
    config_path: str,
    target: str,
) -> _InternalEngineCancelRequest:
    return _InternalEngineCancelRequest(
        command_trace=internal_call_argv(
            api_name=api_name,
            config_path=config_path,
            kwargs={"target": target},
        ),
        target=normalize_text(target),
    )


def _find_cancel_match(
    cfg: Any,
    *,
    target: str,
    queue_entries_with_roots_fn: Callable[[Any], list[tuple[Any, Any]]],
) -> _InternalEngineCancelMatch | None:
    for queue_root, entry in queue_entries_with_roots_fn(cfg):
        if entry.queue_id == target or entry.task_id == target:
            return _InternalEngineCancelMatch(queue_root=queue_root, entry=entry)
    return None


def _cancel_updated_entry(
    match: _InternalEngineCancelMatch,
    *,
    request_cancel_fn: Callable[..., Any | None],
    before_pending_cancel_fn: Callable[[Any], Any] | None = None,
) -> Any | None:
    cancel_kwargs: dict[str, Any] = {"expected_entry": match.entry}
    if before_pending_cancel_fn is not None:
        cancel_kwargs["before_pending_cancel_fn"] = before_pending_cancel_fn
    return request_cancel_fn(
        match.queue_root,
        match.entry.queue_id,
        **cancel_kwargs,
    )


def _recover_cancel_after_error(
    cfg: Any,
    match: _InternalEngineCancelMatch,
    *,
    queue_entries_with_roots_fn: Callable[[Any], list[tuple[Any, Any]]],
) -> Any | None:
    recovered: list[Any] = []
    for queue_root, current in queue_entries_with_roots_fn(cfg):
        if queue_root != match.queue_root or current.queue_id != match.entry.queue_id:
            continue
        if queue_entries_same_generation(current, match.entry):
            recovered.append(current)
    if len(recovered) != 1:
        return None
    current = recovered[0]
    status = _queue_entry_status_text(current).lower()
    if status == STATUS_CANCELLED or (
        status == STATUS_RUNNING and bool(getattr(current, "cancel_requested", False))
    ):
        return current
    return None


def _cancel_success_fields(updated: Any, status: str) -> dict[str, str]:
    return _text_fields(
        {
            "status": status,
            "queue_id": getattr(updated, "queue_id", ""),
            "job_id": getattr(updated, "task_id", ""),
        }
    )


def cancel_internal_engine_target(
    *,
    load_config_fn: Callable[[Any], Any],
    queue_entries_with_roots_fn: Callable[[Any], list[tuple[Any, Any]]],
    request_cancel_fn: Callable[..., Any | None],
    display_status_fn: Callable[[Any], str],
    before_pending_cancel_fn: Callable[[Any], Any] | None = None,
    api_name: str,
    target: str,
    config_path: str,
) -> dict[str, Any]:
    request = _cancel_request(
        api_name=api_name,
        config_path=config_path,
        target=target,
    )
    if not request.target:
        return _cancel_failure_payload(
            command_trace=request.command_trace,
            stderr="queue cancel requires a queue_id or job_id\n",
        )

    try:
        cfg = load_config_fn(config_path)
        match = _find_cancel_match(
            cfg,
            target=request.target,
            queue_entries_with_roots_fn=queue_entries_with_roots_fn,
        )
        if match is None:
            return _cancel_failure_payload(
                command_trace=request.command_trace,
                stderr=f"queue target not found: {request.target}\n",
            )

        try:
            updated = _cancel_updated_entry(
                match,
                request_cancel_fn=request_cancel_fn,
                before_pending_cancel_fn=before_pending_cancel_fn,
            )
        except Exception as cancel_exc:
            try:
                updated = _recover_cancel_after_error(
                    cfg,
                    match,
                    queue_entries_with_roots_fn=queue_entries_with_roots_fn,
                )
            except Exception as reload_exc:
                raise cancel_exc from reload_exc
            if updated is None:
                raise
        if updated is None:
            recovered = _recover_cancel_after_error(
                cfg,
                match,
                queue_entries_with_roots_fn=queue_entries_with_roots_fn,
            )
            if (
                recovered is not None
                and _queue_entry_status_text(recovered).lower() == STATUS_CANCELLED
            ):
                updated = recovered
        if updated is None:
            return _cancel_failure_payload(
                command_trace=request.command_trace,
                stderr=f"queue target already terminal: {request.target}\n",
            )
        status = _direct_cancel_status(updated, display_status_fn(updated))
    except Exception as exc:  # noqa: BLE001
        return _cancel_failure_payload(
            command_trace=request.command_trace,
            stderr=_stderr_with_exception("", exc),
        )

    parsed = _cancel_success_fields(updated, status)
    return _cancel_success_payload(
        command_trace=request.command_trace,
        status=status,
        parsed=parsed,
    )


def cancel_engine_target(
    *,
    spec: InternalEngineSubmitterSpec,
    deps: InternalEngineSubmitterDeps,
    target: str,
    config_path: str,
) -> dict[str, Any]:
    before_pending_cancel_fn = (
        partial(deps.before_pending_cancel_fn, config_path=config_path)
        if deps.before_pending_cancel_fn is not None
        else None
    )
    return cancel_internal_engine_target(
        load_config_fn=deps.load_queue_config_fn,
        queue_entries_with_roots_fn=deps.queue_entries_with_roots_fn,
        request_cancel_fn=deps.request_cancel_fn,
        display_status_fn=deps.display_status_fn,
        before_pending_cancel_fn=before_pending_cancel_fn,
        api_name=spec.cancel_api_name,
        config_path=config_path,
        target=target,
    )
