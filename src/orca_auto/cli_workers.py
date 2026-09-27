"""``queue worker``: find the config, refuse a second worker, then supervise the one worker."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto import cli_worker_supervision
from orca_auto.cli_handlers import CommandConfigError, command_config_path
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.terminal import emit_error, emit_json

LOGGER = logging.getLogger(__name__)


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


def cmd_queue_worker(args: Any) -> int:
    from orca_auto.orca.commands.queue import QUEUE_WORKER_MODULE, existing_worker_pid

    try:
        config_path = command_config_path(args)
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint)
        return 1
    cfg = _load_worker_config(config_path)
    # The supervisor's own interpreter resolves the installed package (editable
    # checkout, wheel, or prepared runtime); no checkout PYTHONPATH is injected.
    argv = [sys.executable, "-m", QUEUE_WORKER_MODULE, "--config", config_path]
    stop_budget = cli_worker_supervision.worker_stop_budget_seconds(
        cfg.runtime.max_concurrent if cfg is not None else None
    )

    if bool(getattr(args, "json", False)):
        emit_json(
            {
                "workers": [
                    {
                        "app": cli_worker_supervision.WORKER_APP,
                        "argv": argv,
                        "cwd": "",
                        "env": None,
                        "restart_on_clean_exit": True,
                        "stop_timeout_seconds": stop_budget,
                    }
                ]
            }
        )
        return 0

    if cfg is not None:
        existing_pid = existing_worker_pid(cfg)
        if existing_pid is not None:
            allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
            command = _format_command_argv(_read_process_command(existing_pid))
            emit_error(
                f"existing ORCA queue worker detected for allowed_root {allowed_root} "
                f"(pid={existing_pid}). command: {command}",
                hint="Stop the existing worker before starting another worker.",
            )
            return 1

    return cli_worker_supervision.run_worker_supervisor(argv, stop_timeout_seconds=stop_budget)
