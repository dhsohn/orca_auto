from __future__ import annotations

import argparse
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto import cli_systemd_units, systemd_plan
from orca_auto.cli_errors import emit_error
from orca_auto.cli_systemd_apply import _run_command
from orca_auto.core.utils.coercion import normalize_text


def _sudo_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("sudo") is not None


# A unit reaches any of these only because someone started it, so restarting it
# honors the opt-in rather than overriding it -- including out of the crash loop
# and tripped start limit a bad deploy leaves behind, which is the state the
# restart exists to clear.
_WORKFLOW_RUNNING_STATES = frozenset({"active", "activating", "reloading", "failed"})
# Stopped, or being stopped by the operator right now: leave it alone.
_WORKFLOW_STOPPED_STATES = frozenset({"inactive", "deactivating", "unknown", ""})
_WORKFLOW_STATE_RETURN_CODES = {
    "active": frozenset({0}),
    "activating": frozenset({0}),
    "reloading": frozenset({0}),
    "failed": frozenset({3}),
    "inactive": frozenset({3}),
    "deactivating": frozenset({0, 3}),
    "unknown": frozenset({3}),
    "": frozenset({3}),
}


def _query_workflow_worker_state(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["systemctl", "is-active", unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ValueError(f"systemctl failed: {exc}") from exc
    stdout = normalize_text(completed.stdout)
    stderr = normalize_text(completed.stderr)
    state = stdout.splitlines()[0] if stdout else ""
    expected_returncodes = _WORKFLOW_STATE_RETURN_CODES.get(state)
    if stderr or expected_returncodes is None or completed.returncode not in expected_returncodes:
        detail = stderr.splitlines()[0] if stderr else state or f"exit {completed.returncode}"
        raise ValueError(f"systemctl answered {detail!r} (exit {completed.returncode})")
    return state


def _restartable_worker_units(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, ...]:
    """Worker services a restart must reload code in, in restart order.

    The workflow worker is opt-in and belongs to no target, so no target restart
    reaches it. It joins the list once it is running, and is never started from
    a stopped state. An unreadable state is not a licence to guess: skipping it
    would report success over a worker still running pre-deploy code.
    """

    units = [systemd_plan._worker_unit_for_user(target_user)]
    workflow_unit = systemd_plan._workflow_worker_unit_for_user(target_user)
    try:
        state = _query_workflow_worker_state(workflow_unit, run=run)
    except ValueError as exc:
        raise ValueError(
            f"cannot tell whether {workflow_unit} is running; {exc}. "
            "Restart it yourself, or rerun once systemctl responds."
        ) from exc
    if state in _WORKFLOW_RUNNING_STATES:
        units.append(workflow_unit)
    elif state not in _WORKFLOW_STOPPED_STATES:
        raise ValueError(
            f"cannot tell whether {workflow_unit} is running; systemctl answered "
            f"{state!r}. Restart it yourself, or rerun once systemctl responds."
        )
    return tuple(units)


def _require_current_restart_units(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    required_units = (systemd_plan._engine_workers_unit_for_user(target_user),)
    missing = tuple(
        unit
        for unit in required_units
        if cli_systemd_units._unit_load_state(unit, run=run) == "not-found"
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
    runtime_unit = systemd_plan._runtime_unit_for_user(target_user)
    engines_unit = systemd_plan._engine_workers_unit_for_user(target_user)
    worker_unit = systemd_plan._worker_unit_for_user(target_user)
    runtime_enabled = cli_systemd_units._query_systemctl("is-enabled", runtime_unit, run=run)
    if runtime_enabled in cli_systemd_units._ENABLED_UNIT_FILE_STATES:
        return runtime_unit
    engines_enabled = cli_systemd_units._query_systemctl("is-enabled", engines_unit, run=run)
    if engines_enabled in cli_systemd_units._ENABLED_UNIT_FILE_STATES:
        return engines_unit
    worker_enabled = cli_systemd_units._query_systemctl("is-enabled", worker_unit, run=run)
    if worker_enabled in cli_systemd_units._ENABLED_UNIT_FILE_STATES:
        return engines_unit
    if not all(
        state in cli_systemd_units._READABLE_UNIT_FILE_STATES
        for state in (runtime_enabled, engines_enabled, worker_enabled)
    ):
        runtime_active = cli_systemd_units._query_systemctl("is-active", runtime_unit, run=run)
        if runtime_active == "active":
            return runtime_unit
    return engines_unit


@dataclass(frozen=True)
class ServiceRestartDeps:
    """Optional overrides for service-restart system effects."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    which: Callable[[str], str | None] | None = None
    is_root: Callable[[], bool] | None = None
    default_service_user: Callable[[], str] | None = None
    restart_unit_for_user: Callable[..., str] | None = None


def _service_target_user(args: argparse.Namespace, deps: ServiceRestartDeps) -> str:
    default_user = deps.default_service_user or cli_systemd_units._default_service_user
    return normalize_text(getattr(args, "target_user", None)) or normalize_text(default_user())


def cmd_service_restart(args: argparse.Namespace, *, deps: ServiceRestartDeps | None = None) -> int:
    deps = deps or ServiceRestartDeps()
    which = deps.which or shutil.which
    run = deps.run or subprocess.run
    is_root = deps.is_root or systemd_plan._is_root
    restart_unit_for_user = deps.restart_unit_for_user or _restart_unit_for_user

    if not cli_systemd_units._systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1
    use_sudo = not is_root()
    if use_sudo and not _sudo_available(which=which):
        emit_error("sudo is required to restart system services; rerun as root")
        return 1

    target_user = _service_target_user(args, deps)
    try:
        unit = restart_unit_for_user(target_user, run=run)
        worker_units = _restartable_worker_units(target_user, run=run)
    except ValueError as exc:
        emit_error(exc)
        return 1

    for reset_unit in worker_units:
        print(f"Resetting service failure state for {reset_unit}")
        rc = _run_command(
            ("systemctl", "reset-failed", reset_unit),
            use_sudo=use_sudo,
            run=run,
        )
        if rc != 0:
            return rc

    print(f"Restarting {unit}")
    rc = _run_command(("systemctl", "restart", unit), use_sudo=use_sudo, run=run)
    if rc != 0:
        return rc

    # The target restart above is not enough to reload the workers. The opt-in
    # workflow worker is structurally out of reach: it belongs to no target. The
    # ORCA worker is a member, but `systemctl restart
    # orca_auto-runtime@<user>.target` still left its ExecMainStartTimestamp
    # untouched on the deploy host. Both import the checkout live and never
    # reload, so restarting the services is what carries a deploy to them --
    # which is what `service status` promises when it reports a stale worker.
    for worker_unit in worker_units:
        print(f"Restarting {worker_unit}")
        rc = _run_command(("systemctl", "restart", worker_unit), use_sudo=use_sudo, run=run)
        if rc != 0:
            return rc

    print("Restart requested successfully.")
    print("Check status with: orca_auto service status")
    return 0


__all__ = ["ServiceRestartDeps", "cmd_service_restart"]
