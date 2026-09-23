"""Worker planning, conflict checks, and dispatch for the ``queue worker`` command."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orca_auto.cli_worker_supervision as cli_worker_supervision
from orca_auto.core.config.discovery import (
    repo_root_for_subprocess,
    resolve_shared_config_path,
    shared_config_text_from_args,
)
from orca_auto.core.terminal import emit_error
from orca_auto.core.utils import normalize_text

LOGGER = logging.getLogger(__name__)

_ENGINE_WORKER_MODULES = {"orca": "orca_auto.core.engines.queue_worker"}
_KNOWN_WORKER_APPS = ("orca",)
_DEFAULT_WORKER_APPS = ("orca",)


def _selected_worker_apps(values: Sequence[str] | None) -> list[str]:
    selected = list(values or [])
    if not selected:
        return list(_DEFAULT_WORKER_APPS)

    ordered: list[str] = []
    seen: set[str] = set()
    for value in selected:
        text = normalize_text(value).lower()
        if not text or text in seen:
            continue
        if text not in _KNOWN_WORKER_APPS:
            raise ValueError(f"Unsupported worker app: {text}")
        seen.add(text)
        ordered.append(text)
    return ordered


def _engine_worker_tail_argv(*, app: str) -> list[str]:
    return ["--engine", app]


def worker_module_command(
    *,
    config_path: str,
    repo_root: str | None,
    module_name: str,
    tail_argv: list[str],
) -> tuple[list[str], str | None, dict[str, str] | None]:
    argv = [sys.executable, "-m", module_name, "--config", config_path, *tail_argv]
    if repo_root is None:
        return argv, None, None

    root_path = Path(repo_root).expanduser().resolve()
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    candidates = [str(root_path)]
    src_root = root_path / "src"
    if src_root.is_dir():
        candidates.insert(0, str(src_root))
    pythonpath = ":".join(candidates)
    env["PYTHONPATH"] = pythonpath if not existing else f"{pythonpath}:{existing}"
    return argv, str(root_path), env


def _engine_worker_spec(
    *,
    app: str,
    config_path: str,
) -> cli_worker_supervision.WorkerSpec:
    argv, cwd, env = worker_module_command(
        config_path=config_path,
        repo_root=repo_root_for_subprocess(),
        module_name=_ENGINE_WORKER_MODULES[app],
        tail_argv=_engine_worker_tail_argv(app=app),
    )
    env_payload = dict(env) if isinstance(env, dict) else None
    return cli_worker_supervision.WorkerSpec(
        app=app,
        argv=tuple(argv),
        cwd=cwd,
        env=env_payload,
    )


def _validate_engine_worker_config(engine_apps: Sequence[str], config_path: str | None) -> None:
    if engine_apps and not normalize_text(config_path):
        raise ValueError(
            "Could not discover orca_auto.yaml for engine workers. Pass --orca_auto-config or set ORCA_AUTO_CONFIG."
        )


def _build_worker_specs(args: Any) -> list[cli_worker_supervision.WorkerSpec]:
    apps = _selected_worker_apps(list(getattr(args, "app", None) or []))
    config_path = resolve_shared_config_path(shared_config_text_from_args(args))
    _validate_engine_worker_config(apps, config_path)
    return [_engine_worker_spec(app=app, config_path=str(config_path)) for app in apps]


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
    return cli_worker_supervision._quoted_command(command_argv)


def _detect_existing_orca_worker_conflict(
    specs: Sequence[cli_worker_supervision.WorkerSpec],
    *,
    args: argparse.Namespace,
) -> _ExistingWorkerConflict | None:
    if not any(spec.app == "orca" for spec in specs):
        return None

    config_path = resolve_shared_config_path(shared_config_text_from_args(args))
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
    print(json.dumps({key: [spec.to_dict() for spec in specs]}, ensure_ascii=True, indent=2))
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

    return cli_worker_supervision._run_worker_supervisor(specs)
