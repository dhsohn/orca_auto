"""Worker planning, conflict checks, and dispatch for the ``queue worker`` command."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orca_auto.cli_worker_supervision as cli_worker_supervision
from orca_auto.core.config.discovery import (
    resolve_shared_config_path,
    shared_config_text_from_args,
)
from orca_auto.core.utils import normalize_text
from orca_auto.terminal import emit_error, emit_json

LOGGER = logging.getLogger(__name__)

ORCA_WORKER_APP = "orca"
ORCA_QUEUE_WORKER_MODULE = "orca_auto.orca.commands.queue"


def worker_module_command(
    *,
    config_path: str,
    module_name: str,
    tail_argv: Sequence[str] = (),
) -> list[str]:
    # The supervisor's own interpreter resolves the installed package (editable
    # checkout, wheel, or prepared runtime); no checkout PYTHONPATH is injected.
    return [sys.executable, "-m", module_name, "--config", config_path, *tail_argv]


def _orca_worker_stop_timeout_seconds(config_path: str) -> float:
    """The worker's shutdown budget for its configured concurrency.

    A config that does not load keeps the spec default; the worker itself then
    fails at startup on the same config and the budget is never exercised.
    """
    from orca_auto.core.queue.processes import worker_shutdown_budget_seconds

    try:
        from orca_auto.orca.config import load_config as _load_orca_config

        cfg = _load_orca_config(config_path)
    except Exception:  # noqa: BLE001
        LOGGER.debug(
            "failed to read the ORCA worker concurrency for its stop budget", exc_info=True
        )
        return cli_worker_supervision.WorkerSpec.stop_timeout_seconds
    return worker_shutdown_budget_seconds(cfg.runtime.max_concurrent)


def _orca_worker_spec(*, config_path: str) -> cli_worker_supervision.WorkerSpec:
    argv = worker_module_command(
        config_path=config_path,
        module_name=ORCA_QUEUE_WORKER_MODULE,
    )
    return cli_worker_supervision.WorkerSpec(
        app=ORCA_WORKER_APP,
        argv=tuple(argv),
        stop_timeout_seconds=_orca_worker_stop_timeout_seconds(config_path),
    )


def _build_worker_specs(args: Any) -> list[cli_worker_supervision.WorkerSpec]:
    config_path = resolve_shared_config_path(shared_config_text_from_args(args))
    if not normalize_text(config_path):
        raise ValueError(
            "Could not discover orca_auto.yaml for the ORCA queue worker. "
            "Pass --orca_auto-config or set ORCA_AUTO_CONFIG."
        )
    return [_orca_worker_spec(config_path=str(config_path))]


@dataclass(frozen=True)
class _ExistingWorkerConflict:
    pid: int
    allowed_root: str
    command: str


def _read_process_command(pid: int) -> tuple[str, ...]:
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        LOGGER.debug("failed to read process command for pid %s", pid, exc_info=True)
        return ()
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    return tuple(parts)


def _format_command_argv(command_argv: Sequence[str]) -> str:
    if not command_argv:
        return "<unavailable>"
    return cli_worker_supervision.quoted_command(command_argv)


def _detect_existing_orca_worker_conflict(
    specs: Sequence[cli_worker_supervision.WorkerSpec],
    *,
    args: argparse.Namespace,
) -> _ExistingWorkerConflict | None:
    if not any(spec.app == ORCA_WORKER_APP for spec in specs):
        return None

    config_path = resolve_shared_config_path(shared_config_text_from_args(args))
    if not normalize_text(config_path):
        return None

    try:
        from orca_auto.orca.config import load_config as _load_orca_config
        from orca_auto.orca.queue.orphans import read_worker_pid as _read_orca_worker_pid

        cfg = _load_orca_config(str(config_path))
    except Exception:  # noqa: BLE001
        LOGGER.debug("failed to inspect existing ORCA worker config", exc_info=True)
        return None

    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    existing_pid = _read_orca_worker_pid(allowed_root)
    if existing_pid is None:
        return None

    command_argv = _read_process_command(existing_pid)
    return _ExistingWorkerConflict(
        pid=existing_pid,
        allowed_root=str(allowed_root),
        command=_format_command_argv(command_argv),
    )


def _emit_existing_orca_worker_conflict(
    conflict: _ExistingWorkerConflict,
) -> int:
    emit_error(
        f"existing ORCA queue worker detected for allowed_root {conflict.allowed_root} "
        f"(pid={conflict.pid}). command: {conflict.command}",
        hint="Stop the existing worker before starting another worker.",
    )
    return 1


def _emit_supervisor_specs_json(
    *,
    key: str,
    specs: Sequence[cli_worker_supervision.WorkerSpec],
) -> int:
    emit_json({key: [spec.to_dict() for spec in specs]})
    return 0


def cmd_queue_worker(args: Any) -> int:
    try:
        specs = _build_worker_specs(args)
    except ValueError as exc:
        emit_error(exc)
        return 1

    if bool(getattr(args, "json", False)):
        return _emit_supervisor_specs_json(key="workers", specs=specs)

    conflict = _detect_existing_orca_worker_conflict(specs, args=args)
    if conflict is not None:
        return _emit_existing_orca_worker_conflict(conflict)

    return cli_worker_supervision.run_worker_supervisor(specs)
