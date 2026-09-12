from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.commands.queue import display_status
from orca_auto.core.engines import command_result as _engine_models
from orca_auto.core.utils import normalize_text as _normalize_text

_CANCEL_API_NAME = "orca_auto.orca.direct_cancel"


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
