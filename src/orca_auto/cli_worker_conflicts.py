from __future__ import annotations

import argparse
import logging
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orca_auto.cli_common import (
    _discover_shared_config_path,
    _effective_shared_config_text,
)
from orca_auto.cli_worker_specs import _ENGINE_WORKER_MODULES, WorkerSpec
from orca_auto.core.utils import normalize_text

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExistingWorkerConflict:
    app: str
    pid: int
    allowed_root: str
    source: str
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


def _command_invokes_module(command_argv: Sequence[str], module_name: str) -> bool:
    target = normalize_text(module_name).lower()
    if not target:
        return False

    normalized = [normalize_text(part).lower() for part in command_argv]
    for index, part in enumerate(normalized[:-1]):
        if part == "-m" and normalized[index + 1] == target:
            return True
    return False


def _command_program_name(command_argv: Sequence[str]) -> str:
    if not command_argv:
        return ""
    raw = normalize_text(command_argv[0])
    if not raw:
        return ""
    return Path(raw).stem.lower()


def _classify_existing_orca_worker(command_argv: Sequence[str]) -> str:
    program_name = _command_program_name(command_argv)
    if (
        program_name == "orca_auto"
        or _command_invokes_module(command_argv, "orca_auto.cli")
        or _command_invokes_module(command_argv, "orca_auto.orca.commands.queue")
        or _command_invokes_module(command_argv, _ENGINE_WORKER_MODULES["orca"])
    ):
        return "orca_auto"
    return "unknown"


def _format_command_argv(command_argv: Sequence[str]) -> str:
    if not command_argv:
        return "<unavailable>"
    return _quoted_command(command_argv)


def _quoted_command(command_argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command_argv)


def _detect_existing_orca_worker_conflict(
    specs: Sequence[WorkerSpec],
    *,
    args: argparse.Namespace,
) -> _ExistingWorkerConflict | None:
    if not any(spec.app == "orca" for spec in specs):
        return None

    config_path = _discover_shared_config_path(_effective_shared_config_text(args))
    if not normalize_text(config_path):
        return None

    try:
        from orca_auto.orca.config import load_config as _load_orca_config
        from orca_auto.orca.engine import read_worker_pid as _read_orca_worker_pid

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
        app="orca",
        pid=existing_pid,
        allowed_root=str(allowed_root),
        source=_classify_existing_orca_worker(command_argv),
        command=_format_command_argv(command_argv),
    )


def _emit_existing_orca_worker_conflict(
    conflict: _ExistingWorkerConflict,
    *,
    command_name: str,
) -> int:
    del command_name
    print(
        f"error: existing ORCA queue worker detected for allowed_root {conflict.allowed_root} "
        f"(pid={conflict.pid})."
    )
    if conflict.source == "orca_auto":
        print("source: orca_auto queue worker")
        print("This queue root is already being managed by a running orca_auto worker.")
    else:
        print("source: existing queue worker")
    print(f"command: {conflict.command}")
    if conflict.source == "orca_auto":
        print("Stop the existing queue-worker service before starting another worker.")
    else:
        print("Stop the existing worker before starting another worker.")
    return 1
