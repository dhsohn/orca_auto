from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_common import (
    _discover_shared_config_path,
    _effective_shared_config_text,
    _repo_root_for_subprocess,
    _workflow_root_for_args,
)
from orca_auto.core.app_ids import (
    ORCA_AUTO_CONFIG_ENV_VAR,
    ORCA_AUTO_WORKFLOW_WORKER_MODULE,
)
from orca_auto.core.utils import normalize_text

_WORKFLOW_ENGINE_APPS = ("crest", "xtb")
_ENGINE_APPS = ("orca",)
_ENGINE_WORKER_MODULES = {
    "orca": "orca_auto.core.engines.queue_worker",
    "crest": "orca_auto.core.engines.queue_worker",
    "xtb": "orca_auto.core.engines.queue_worker",
}
_KNOWN_WORKER_APPS = (*_ENGINE_APPS, "workflow")
_DEFAULT_WORKER_APPS = _ENGINE_APPS


@dataclass(frozen=True)
class WorkerSpec:
    app: str
    argv: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] | None = None
    restart_on_clean_exit: bool = True

    def to_dict(self) -> dict[str, Any]:
        env_payload: dict[str, str] | None = None
        if isinstance(self.env, dict):
            allowed_env_keys = (ORCA_AUTO_CONFIG_ENV_VAR, "PYTHONPATH")
            env_payload = {}
            for key in allowed_env_keys:
                value = normalize_text(self.env.get(key))
                if value:
                    env_payload[key] = value
            if not env_payload:
                env_payload = None
        return {
            "app": self.app,
            "argv": list(self.argv),
            "cwd": self.cwd or "",
            "env": env_payload,
            "restart_on_clean_exit": self.restart_on_clean_exit,
        }


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
) -> WorkerSpec:
    argv, cwd, env = worker_module_command(
        config_path=config_path,
        repo_root=_repo_root_for_subprocess(),
        module_name=_ENGINE_WORKER_MODULES[app],
        tail_argv=_engine_worker_tail_argv(app=app),
    )
    env_payload = dict(env) if isinstance(env, dict) else None
    return WorkerSpec(app=app, argv=tuple(argv), cwd=cwd, env=env_payload)


def _workflow_worker_spec(
    *,
    workflow_root: str,
    config_path: str | None,
    args: argparse.Namespace,
) -> WorkerSpec:
    argv = [
        sys.executable,
        "-m",
        ORCA_AUTO_WORKFLOW_WORKER_MODULE,
        "--workflow-root",
        str(Path(workflow_root).expanduser().resolve()),
    ]
    if normalize_text(config_path):
        argv.extend(["--orca_auto-config", str(Path(str(config_path)).expanduser().resolve())])
    if bool(getattr(args, "no_submit", False)):
        argv.append("--no-submit")
    if bool(getattr(args, "once", False)):
        argv.append("--once")
    if bool(getattr(args, "refresh_registry", False)):
        argv.append("--refresh-registry")
    if bool(getattr(args, "refresh_each_cycle", False)):
        argv.append("--refresh-each-cycle")

    max_cycles = int(getattr(args, "max_cycles", 0) or 0)
    if max_cycles > 0:
        argv.extend(["--max-cycles", str(max_cycles)])

    interval_seconds = float(getattr(args, "interval_seconds", 0.0) or 0.0)
    if interval_seconds > 0:
        argv.extend(["--interval-seconds", str(interval_seconds)])

    lock_timeout_seconds = float(getattr(args, "lock_timeout_seconds", 0.0) or 0.0)
    if lock_timeout_seconds > 0:
        argv.extend(["--lock-timeout-seconds", str(lock_timeout_seconds)])
    finite_worker = bool(getattr(args, "once", False)) or max_cycles > 0
    return WorkerSpec(
        app="workflow",
        argv=tuple(argv),
        restart_on_clean_exit=not finite_worker,
    )


def _worker_engine_apps(apps: Sequence[str], *, workflow_enabled: bool) -> list[str]:
    engine_apps = [app for app in apps if app in _ENGINE_APPS]
    if workflow_enabled:
        for app in _WORKFLOW_ENGINE_APPS:
            if app not in engine_apps:
                engine_apps.append(app)
    return engine_apps


def _validate_engine_worker_config(engine_apps: Sequence[str], config_path: str | None) -> None:
    if engine_apps and not normalize_text(config_path):
        raise ValueError(
            "Could not discover orca_auto.yaml for engine workers. Pass --orca_auto-config or set ORCA_AUTO_CONFIG."
        )


def _workflow_only_worker_flag_error(args: Any) -> str | None:
    if any(
        bool(getattr(args, attr, False))
        for attr in ("no_submit", "refresh_registry", "refresh_each_cycle")
    ):
        raise ValueError("workflow-only worker flags require --app workflow")
    numeric_flags = (
        ("max_cycles", int, "--max-cycles"),
        ("interval_seconds", float, "--interval-seconds"),
        ("lock_timeout_seconds", float, "--lock-timeout-seconds"),
    )
    for attr, caster, option in numeric_flags:
        if caster(getattr(args, attr, 0) or 0) > 0:
            return f"{option} requires --app workflow"
    return None


def _add_workflow_worker_spec(
    specs: list[WorkerSpec],
    *,
    apps: Sequence[str],
    explicit_app_selection: bool,
    workflow_root: str | None,
    config_path: str | None,
    args: argparse.Namespace,
) -> None:
    if "workflow" in apps and not workflow_root:
        raise ValueError("workflow worker requires runs_root in orca_auto.yaml")

    should_add_workflow = "workflow" in apps or (not explicit_app_selection and bool(workflow_root))
    if should_add_workflow and workflow_root:
        specs.append(
            _workflow_worker_spec(workflow_root=workflow_root, config_path=config_path, args=args)
        )
        return

    flag_error = _workflow_only_worker_flag_error(args)
    if flag_error:
        raise ValueError(flag_error)


def _build_worker_specs(args: Any) -> list[WorkerSpec]:
    explicit_apps = list(getattr(args, "app", None) or [])
    apps = _selected_worker_apps(explicit_apps)
    explicit_app_selection = bool(explicit_apps)
    config_path = _discover_shared_config_path(_effective_shared_config_text(args))
    workflow_root = _workflow_root_for_args(args)
    workflow_enabled = "workflow" in apps or (not explicit_app_selection and bool(workflow_root))
    engine_apps = _worker_engine_apps(apps, workflow_enabled=workflow_enabled)
    _validate_engine_worker_config(engine_apps, config_path)

    specs = [_engine_worker_spec(app=app, config_path=str(config_path)) for app in engine_apps]
    _add_workflow_worker_spec(
        specs,
        apps=apps,
        explicit_app_selection=explicit_app_selection,
        workflow_root=workflow_root,
        config_path=config_path,
        args=args,
    )
    return specs
