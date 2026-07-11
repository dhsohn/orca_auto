from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_errors import emit_error
from orca_auto.systemd_plan import (
    DEFAULT_SYSTEMD_UNIT_DIR,
    SystemdInstallPlan,
    _format_command,
    _is_root,
    _print_plan,
    _print_warnings,
    _systemd_command_argv,
    build_systemd_install_plan,
)


def _run_command(
    command: Sequence[str],
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    argv = _systemd_command_argv(command, use_sudo=use_sudo)
    print(f"$ {_format_command(command, use_sudo=use_sudo)}")
    completed = run(argv, check=False)
    return int(completed.returncode)


def _write_units_direct(plan: SystemdInstallPlan) -> None:
    plan.unit_dir.mkdir(parents=True, exist_ok=True)
    for unit in plan.units:
        unit.destination.write_text(unit.content, encoding="utf-8")
        unit.destination.chmod(0o644)
        print(f"installed: {unit.destination}")


def _write_units_with_sudo(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    with tempfile.TemporaryDirectory(prefix="orca_auto-systemd-") as tmp_dir_text:
        tmp_dir = Path(tmp_dir_text)
        for unit in plan.units:
            (tmp_dir / unit.name).write_text(unit.content, encoding="utf-8")

        rc = _run_command(("mkdir", "-p", str(plan.unit_dir)), use_sudo=True, run=run)
        if rc != 0:
            return rc
        for unit in plan.units:
            rc = _run_command(
                ("install", "-m", "0644", str(tmp_dir / unit.name), str(unit.destination)),
                use_sudo=True,
                run=run,
            )
            if rc != 0:
                return rc
            print(f"installed: {unit.destination}")
    return 0


def apply_systemd_install_plan(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    python_path = plan.repo / ".venv" / "bin" / "python"
    if not python_path.is_file() or not os.access(python_path, os.X_OK):
        emit_error(f"service Python is missing or not executable: {python_path}; run `make venv`")
        return 1

    if plan.use_sudo and shutil.which("sudo") is None:
        emit_error("sudo is required to write system units; rerun as root or use --no-sudo")
        return 1

    if plan.use_sudo:
        rc = _write_units_with_sudo(plan, run=run)
    else:
        try:
            _write_units_direct(plan)
        except OSError as exc:
            emit_error(f"failed to write systemd units: {exc}")
            return 1
        rc = 0
    if rc != 0:
        return rc

    for command in plan.commands:
        rc = _run_command(command, use_sudo=plan.use_sudo, run=run)
        if rc != 0:
            return rc

    if plan.enabled_unit:
        print(f"enabled: {plan.enabled_unit}")
    else:
        print("installed systemd units; enable/start skipped")
    return 0


@dataclass(frozen=True)
class SystemdInstallCliDeps:
    """Optional overrides for system-effect seams (test injection)."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    is_root: Callable[[], bool] | None = None


def cmd_systemd_install(
    args: argparse.Namespace, *, deps: SystemdInstallCliDeps | None = None
) -> int:
    deps = deps or SystemdInstallCliDeps()
    run = deps.run or subprocess.run
    is_root = deps.is_root or _is_root

    try:
        plan = build_systemd_install_plan(
            target_user=getattr(args, "target_user", None),
            repo=getattr(args, "repo", None),
            config=getattr(args, "config", None),
            unit_dir=getattr(args, "unit_dir", DEFAULT_SYSTEMD_UNIT_DIR),
            worker_only=bool(getattr(args, "worker_only", False)),
            no_enable=bool(getattr(args, "no_enable", False)),
            no_start=bool(getattr(args, "no_start", False)),
            no_sudo=bool(getattr(args, "no_sudo", False)),
            is_root=is_root,
        )
    except (OSError, ValueError) as exc:
        emit_error(exc)
        return 1

    _print_warnings(plan)
    if bool(getattr(args, "dry_run", False)):
        _print_plan(plan)
        return 0
    return int(apply_systemd_install_plan(plan, run=run))


__all__ = [
    "SystemdInstallCliDeps",
    "apply_systemd_install_plan",
    "cmd_systemd_install",
]
