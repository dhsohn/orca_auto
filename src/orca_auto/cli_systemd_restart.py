from __future__ import annotations

import argparse
import shutil
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from orca_auto import cli_systemd_units, systemd_plan
from orca_auto.cli_systemd_restart_guard import guard_service_restart
from orca_auto.core.terminal import emit_error


def _sudo_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("sudo") is not None


def _restartable_worker_units(target_user: str) -> tuple[str, ...]:
    """Return the ORCA worker that must reload code after a target restart."""
    return (dict(cli_systemd_units.service_units_for_user(target_user))["worker"],)


def _require_current_restart_units(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    required_units = (dict(cli_systemd_units.service_units_for_user(target_user))["engines"],)
    missing = tuple(
        unit
        for unit in required_units
        if cli_systemd_units.unit_load_state(unit, run=run) == "not-found"
    )
    if not missing:
        return
    raise ValueError(
        "required systemd units are not installed: "
        f"{', '.join(missing)}. Rerun the installer for this checkout: "
        f"orca_auto systemd install --user {target_user} --repo <repo>"
    )


def _restart_unit_for_user(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    _require_current_restart_units(target_user, run=run)
    units_by_role = dict(cli_systemd_units.service_units_for_user(target_user))

    def enabled_state(label: str) -> str | None:
        return cli_systemd_units.query_systemctl("is-enabled", units_by_role[label], run=run)

    mode = cli_systemd_units.select_service_mode(
        enabled_state=enabled_state,
        runtime_active=lambda: (
            cli_systemd_units.query_systemctl("is-active", units_by_role["runtime"], run=run)
            == "active"
        ),
    )
    return units_by_role["runtime"] if mode == "full" else units_by_role["engines"]


@dataclass(frozen=True)
class ServiceRestartDeps:
    """Optional overrides for service-restart system effects."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    which: Callable[[str], str | None] | None = None
    is_root: Callable[[], bool] | None = None
    default_service_user: Callable[[], str] | None = None
    restart_unit_for_user: Callable[..., str] | None = None
    restart_guard: Callable[..., AbstractContextManager[None]] | None = None


def cmd_service_restart(args: argparse.Namespace, *, deps: ServiceRestartDeps | None = None) -> int:
    deps = deps or ServiceRestartDeps()
    which = deps.which or shutil.which
    run = deps.run or subprocess.run
    is_root = deps.is_root or systemd_plan._is_root
    restart_unit_for_user = deps.restart_unit_for_user or _restart_unit_for_user

    if not cli_systemd_units.systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1
    use_sudo = not is_root()
    if use_sudo and not _sudo_available(which=which):
        emit_error("sudo is required to restart system services; rerun as root")
        return 1

    target_user = cli_systemd_units.service_target_user(
        args, default_user=deps.default_service_user
    )
    try:
        unit = restart_unit_for_user(target_user, run=run)
        worker_units = _restartable_worker_units(target_user)
    except ValueError as exc:
        emit_error(exc)
        return 1

    if use_sudo:
        # Authenticate before blocking admission. Mutation commands must not
        # prompt while workers are waiting for the shared pool lock.
        try:
            authenticated = run(["sudo", "-v"], check=False)
        except OSError as exc:
            emit_error(f"sudo authentication failed: {exc}")
            return 1
        if authenticated.returncode != 0:
            return int(authenticated.returncode)

    def mutation_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if use_sudo and argv[0] == "sudo":
            argv = [argv[0], "-n", *argv[1:]]
        return run(argv, **kwargs)

    force = bool(getattr(args, "force", False))
    if force:
        print("WARNING: --force can interrupt running calculations and consume recovery attempts.")
    guard = (
        nullcontext()
        if force
        else (deps.restart_guard or guard_service_restart)(worker_units, run=run)
    )
    mutation_started = False
    try:
        with guard:
            mutation_started = True
            return _restart_selected_units(unit, worker_units, use_sudo=use_sudo, run=mutation_run)
    except (OSError, ValueError) as exc:
        detail = str(exc).rstrip(". ")
        if mutation_started:
            emit_error(
                f"Restart failed: {detail}. Some services may have changed; check service status."
            )
        else:
            emit_error(
                f"Restart refused: {detail}. No services were changed. "
                "Wait for an idle window and check the installed service configuration. "
                "Use orca_auto service restart --force only to accept possible calculation interruption."
            )
        return 1


def _restart_selected_units(
    unit: str,
    worker_units: tuple[str, ...],
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    for reset_unit in worker_units:
        print(f"Resetting service failure state for {reset_unit}")
        rc = cli_systemd_units.run_command(
            ("systemctl", "reset-failed", reset_unit),
            use_sudo=use_sudo,
            run=run,
        )
        if rc != 0:
            return rc

    print(f"Restarting {unit}")
    rc = cli_systemd_units.run_command(("systemctl", "restart", unit), use_sudo=use_sudo, run=run)
    if rc != 0:
        return rc

    # Restarting a target does not reload its already-running ORCA worker.
    # Restart the service explicitly so the selected runtime reaches the process.
    for worker_unit in worker_units:
        print(f"Restarting {worker_unit}")
        rc = cli_systemd_units.run_command(
            ("systemctl", "restart", worker_unit), use_sudo=use_sudo, run=run
        )
        if rc != 0:
            return rc

    print("Restart requested successfully.")
    print("Check status with: orca_auto service status")
    return 0


__all__ = ["ServiceRestartDeps", "cmd_service_restart"]
