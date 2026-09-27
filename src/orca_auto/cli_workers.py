"""Worker planning, conflict checks, and dispatch for the ``queue worker`` command."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orca_auto.cli_worker_supervision as cli_worker_supervision
from orca_auto.cli_handlers import CommandConfigError, command_config_path
from orca_auto.core.queue.processes import worker_shutdown_budget_seconds
from orca_auto.core.queue.worker.pid_file import read_worker_pid_file
from orca_auto.orca.config import AppConfig, load_config
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


def _load_worker_config(config_path: str) -> AppConfig | None:
    """The worker's config, or None when it does not load.

    The supervisor still starts the worker, which then fails at startup on the
    same config; the stop budget keeps its default and no PID file is checked.
    """
    try:
        return load_config(config_path)
    except Exception:  # noqa: BLE001
        LOGGER.debug("failed to load the ORCA worker config", exc_info=True)
        return None


def _orca_worker_spec(
    *, config_path: str, cfg: AppConfig | None
) -> cli_worker_supervision.WorkerSpec:
    argv = worker_module_command(
        config_path=config_path,
        module_name=ORCA_QUEUE_WORKER_MODULE,
    )
    if cfg is None:
        return cli_worker_supervision.WorkerSpec(app=ORCA_WORKER_APP, argv=tuple(argv))
    return cli_worker_supervision.WorkerSpec(
        app=ORCA_WORKER_APP,
        argv=tuple(argv),
        stop_timeout_seconds=worker_shutdown_budget_seconds(cfg.runtime.max_concurrent),
    )


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


def _detect_existing_orca_worker_conflict(cfg: AppConfig | None) -> _ExistingWorkerConflict | None:
    if cfg is None:
        return None
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    existing_pid = read_worker_pid_file(allowed_root)
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
        config_path = command_config_path(args)
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint)
        return 1
    cfg = _load_worker_config(config_path)
    specs = [_orca_worker_spec(config_path=config_path, cfg=cfg)]

    if bool(getattr(args, "json", False)):
        return _emit_supervisor_specs_json(key="workers", specs=specs)

    conflict = _detect_existing_orca_worker_conflict(cfg)
    if conflict is not None:
        return _emit_existing_orca_worker_conflict(conflict)

    return cli_worker_supervision.run_worker_supervisor(specs)
