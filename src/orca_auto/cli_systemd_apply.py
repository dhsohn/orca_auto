from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_systemd_units import _run_command
from orca_auto.core.terminal import emit_error
from orca_auto.systemd_plan import (
    DEFAULT_SYSTEMD_UNIT_DIR,
    SystemdInstallPlan,
    _is_root,
    _print_plan,
    _print_warnings,
    build_systemd_install_plan,
)


def _write_unit_files(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    if plan.use_sudo:
        rc = _run_command(("mkdir", "-p", str(plan.unit_dir)), use_sudo=True, run=run)
        if rc != 0:
            return rc
        with tempfile.TemporaryDirectory(prefix="orca_auto-systemd-") as staging_text:
            staging = Path(staging_text)
            for unit in plan.units:
                staged = staging / unit.name
                staged.write_text(unit.content, encoding="utf-8")
                staged.chmod(0o644)
                rc = _run_command(
                    ("install", "-m", "0644", str(staged), str(unit.destination)),
                    use_sudo=True,
                    run=run,
                )
                if rc != 0:
                    return rc
                print(f"installed: {unit.destination}")
        return 0
    plan.unit_dir.mkdir(parents=True, exist_ok=True)
    for unit in plan.units:
        # Swap each rendered file in atomically so systemd never reads a
        # half-written unit; a failed install is repaired by rerunning it.
        staged = unit.destination.with_name(unit.destination.name + ".tmp")
        staged.write_text(unit.content, encoding="utf-8")
        staged.chmod(0o644)
        os.replace(staged, unit.destination)
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

    try:
        rc = _write_unit_files(plan, run=run)
    except OSError as exc:
        emit_error(f"failed to write systemd units: {exc}")
        return 1
    if rc != 0:
        return rc

    for command in plan.commands:
        try:
            rc = _run_command(command, use_sudo=plan.use_sudo, run=run)
        except OSError as exc:
            emit_error(
                "systemd install command failed after unit files were updated: "
                f"{exc}; fix the failure and rerun `orca_auto systemd install`"
            )
            return 1
        if rc != 0:
            emit_error(
                "systemd install command failed after unit files were updated; "
                "fix the failure and rerun `orca_auto systemd install`"
            )
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
