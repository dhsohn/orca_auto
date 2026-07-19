from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Any, cast

from orca_auto.cli_common import (
    _shared_orca_auto_config,
    _workflow_root_for_args,
)
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config.files import config_env_value
from orca_auto.core.utils import now_utc_iso, timestamped_token
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.lock import file_lock

from ..engine_options import WorkflowEngineOptions
from ..registry import (
    append_workflow_journal_event,
    write_workflow_worker_state,
)
from ..runtime import (
    advance_workflow_registry_once,
    workflow_lease_expires_at,
    workflow_worker_lock_path,
)
from . import workflow_output as _workflow_output
from .worker_options import WorkflowWorkerOptionConfig, add_workflow_worker_cli_options


def _emit_worker_payload(payload: dict[str, Any], *, json_mode: bool, single_cycle: bool) -> None:
    _workflow_output.emit_worker_payload(payload, json_mode=json_mode, single_cycle=single_cycle)


@dataclass(frozen=True)
class _WorkflowWorkerOptions:
    max_cycles: int
    interval_seconds: float
    lock_timeout_seconds: float
    refresh_registry: bool
    refresh_each_cycle: bool
    service_mode: bool
    json_mode: bool
    workflow_root: Any
    workflow_root_text: str
    worker_session_id: str
    lease_seconds: float
    submit_ready: bool
    engines: WorkflowEngineOptions


def _workflow_worker_options(args: Any) -> _WorkflowWorkerOptions:
    once = bool(getattr(args, "once", False))
    max_cycles = int(getattr(args, "max_cycles", 0) or 0)
    if once:
        max_cycles = 1
    if max_cycles < 0:
        raise ValueError("--max-cycles must be >= 0")

    interval_seconds = float(getattr(args, "interval_seconds", 30.0) or 30.0)
    shared_config = _shared_orca_auto_config(args)
    workflow_root = _workflow_root_for_args(args, config_path=shared_config)
    if not workflow_root:
        raise ValueError(
            "workflow_root is not configured. Pass --workflow-root or set runs_root in "
            "orca_auto.yaml."
        )

    worker_session_id = normalize_text(getattr(args, "worker_session_id", "")) or timestamped_token(
        "wf_worker"
    )
    return _WorkflowWorkerOptions(
        max_cycles=max_cycles,
        interval_seconds=interval_seconds,
        lock_timeout_seconds=float(getattr(args, "lock_timeout_seconds", 5.0) or 5.0),
        refresh_registry=bool(getattr(args, "refresh_registry", False)),
        refresh_each_cycle=bool(getattr(args, "refresh_each_cycle", False)),
        service_mode=bool(getattr(args, "service_mode", False)),
        json_mode=bool(getattr(args, "json", False)),
        workflow_root=workflow_root,
        workflow_root_text=str(workflow_root),
        worker_session_id=worker_session_id,
        lease_seconds=max(
            float(getattr(args, "lease_seconds", 60.0) or 60.0),
            interval_seconds * 2.5,
        ),
        submit_ready=not bool(getattr(args, "no_submit", False)),
        engines=WorkflowEngineOptions.from_values(
            shared_config=shared_config,
            orca_repo_root=getattr(args, "orca_repo_root", None),
        ),
    )


def _write_workflow_worker_status(
    options: _WorkflowWorkerOptions,
    *,
    status: str,
    **kwargs: Any,
) -> None:
    write_workflow_worker_state(
        options.workflow_root_text,
        worker_session_id=options.worker_session_id,
        status=status,
        workflow_root_path=options.workflow_root_text,
        interval_seconds=options.interval_seconds,
        submit_ready=options.submit_ready,
        **kwargs,
    )


def _append_workflow_worker_event(
    options: _WorkflowWorkerOptions,
    *,
    event_type: str,
    **kwargs: Any,
) -> None:
    append_workflow_journal_event(
        options.workflow_root_text,
        event_type=event_type,
        worker_session_id=options.worker_session_id,
        **kwargs,
    )


def _advance_workflow_worker_cycle(
    options: _WorkflowWorkerOptions,
    *,
    cycle_count: int,
) -> dict[str, Any]:
    engines = options.engines
    # WorkflowRegistryCyclePayload is a TypedDict; downstream emitters take dict[str, Any].
    return cast(
        "dict[str, Any]",
        advance_workflow_registry_once(
            workflow_root=options.workflow_root_text,
            shared_config=engines.shared_config,
            orca_repo_root=engines.orca.repo_root,
            submit_ready=options.submit_ready,
            refresh_registry=options.refresh_each_cycle
            or (options.refresh_registry and cycle_count == 1),
            worker_session_id=options.worker_session_id,
            interval_seconds=options.interval_seconds,
            lease_seconds=options.lease_seconds,
        ),
    )


def _record_workflow_worker_started(options: _WorkflowWorkerOptions) -> None:
    started_at = now_utc_iso()
    _write_workflow_worker_status(
        options,
        status="running",
        last_heartbeat_at=started_at,
        lease_expires_at=workflow_lease_expires_at(options.lease_seconds),
    )
    _append_workflow_worker_event(
        options,
        event_type="worker_started",
        metadata={"started_at": started_at, "service_mode": options.service_mode},
    )


def _record_workflow_worker_heartbeat(
    options: _WorkflowWorkerOptions,
    payload: dict[str, Any],
) -> None:
    cycle_finished_at = normalize_text(payload.get("cycle_finished_at")) or now_utc_iso()
    metadata = {
        "discovered_count": int(payload.get("discovered_count", 0) or 0),
        "advanced_count": int(payload.get("advanced_count", 0) or 0),
        "skipped_count": int(payload.get("skipped_count", 0) or 0),
        "failed_count": int(payload.get("failed_count", 0) or 0),
    }
    if bool(payload.get("admission_blocked")):
        metadata["admission_blocked"] = True
    _write_workflow_worker_status(
        options,
        status="running",
        last_cycle_started_at=normalize_text(payload.get("cycle_started_at")),
        last_cycle_finished_at=cycle_finished_at,
        last_heartbeat_at=cycle_finished_at,
        lease_expires_at=workflow_lease_expires_at(options.lease_seconds),
        metadata=metadata,
        minimum_heartbeat_interval_seconds=min(60.0, max(1.0, options.lease_seconds / 2.0)),
    )


def _record_workflow_worker_stopped(
    options: _WorkflowWorkerOptions,
    *,
    cycle_count: int,
) -> None:
    stopped_at = now_utc_iso()
    _write_workflow_worker_status(
        options,
        status="stopped",
        last_cycle_finished_at=stopped_at,
        last_heartbeat_at=stopped_at,
        metadata={
            "stop_reason": "max_cycles_reached",
            "cycle_count": cycle_count,
            "service_mode": options.service_mode,
        },
    )
    _append_workflow_worker_event(
        options,
        event_type="worker_stopped",
        metadata={
            "stopped_at": stopped_at,
            "reason": "max_cycles_reached",
            "cycle_count": cycle_count,
        },
    )


def _record_workflow_worker_interrupted(
    options: _WorkflowWorkerOptions,
    *,
    cycle_count: int,
) -> None:
    stopped_at = now_utc_iso()
    _write_workflow_worker_status(
        options,
        status="interrupted",
        last_heartbeat_at=stopped_at,
        metadata={
            "stop_reason": "keyboard_interrupt",
            "cycle_count": cycle_count,
            "service_mode": options.service_mode,
        },
    )
    _append_workflow_worker_event(
        options,
        event_type="worker_interrupted",
        metadata={"stopped_at": stopped_at, "cycle_count": cycle_count},
    )


def _record_workflow_worker_lock_error(
    options: _WorkflowWorkerOptions,
    *,
    error: TimeoutError,
) -> None:
    stopped_at = now_utc_iso()
    _append_workflow_worker_event(
        options,
        event_type="worker_lock_error",
        reason=str(error),
        metadata={"stopped_at": stopped_at},
    )


def _run_workflow_worker_loop(options: _WorkflowWorkerOptions) -> int:
    cycle_count = 0
    try:
        with file_lock(
            workflow_worker_lock_path(options.workflow_root),
            timeout_seconds=options.lock_timeout_seconds,
        ):
            _record_workflow_worker_started(options)
            while True:
                cycle_count += 1
                payload = _advance_workflow_worker_cycle(
                    options,
                    cycle_count=cycle_count,
                )
                _record_workflow_worker_heartbeat(options, payload)
                _emit_worker_payload(
                    payload,
                    json_mode=options.json_mode,
                    single_cycle=options.max_cycles == 1,
                )
                if options.max_cycles > 0 and cycle_count >= options.max_cycles:
                    _record_workflow_worker_stopped(
                        options,
                        cycle_count=cycle_count,
                    )
                    return 0
                time.sleep(max(0.0, options.interval_seconds))
    except KeyboardInterrupt:
        _record_workflow_worker_interrupted(options, cycle_count=cycle_count)
        return 130
    except TimeoutError as exc:
        _record_workflow_worker_lock_error(options, error=exc)
        _workflow_output.emit_worker_lock_error(exc)
        return 1


def cmd_workflow_worker(args: Any) -> int:
    try:
        options = _workflow_worker_options(args)
    except ValueError as exc:
        _workflow_output.emit_error(exc)
        return 1
    return _run_workflow_worker_loop(options)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.cli.workflow")
    add_workflow_worker_cli_options(
        parser,
        config=WorkflowWorkerOptionConfig(
            workflow_root_required=True,
            workflow_root_help="Root that directly contains workflow workspaces.",
            orca_auto_config_flags=("--orca_auto-config",),
            orca_auto_config_default=config_env_value(ORCA_AUTO_CONFIG_ENV_VAR) or None,
            no_submit_help="Only sync/append stages; do not submit newly actionable stages.",
            include_once=True,
            refresh_registry_help="Reindex the workflow registry before the first cycle.",
            refresh_each_cycle_help="Reindex the workflow registry before every cycle.",
            max_cycles_help="Optional cycle limit; 0 means run forever.",
            interval_seconds_default=30.0,
            interval_seconds_help="Sleep interval between orchestration cycles.",
            lock_timeout_seconds_default=5.0,
            lock_timeout_seconds_help="How long to wait for the worker lock.",
            json_help="Print JSON output.",
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return int(cmd_workflow_worker(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
