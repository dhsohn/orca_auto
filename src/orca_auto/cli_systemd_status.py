from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto import cli_style
from orca_auto.cli_errors import emit_error
from orca_auto.cli_systemd_apply import _run_command
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.systemd_plan import _is_root

SERVICE_UNIT_ORDER = (
    ("runtime", "orca_auto-runtime@{user}.target"),
    ("worker", "orca_auto-queue-worker@{user}.service"),
    ("bot", "orca_auto-bot@{user}.service"),
)


@dataclass(frozen=True)
class ServiceUnitStatus:
    label: str
    unit: str
    active: str
    enabled: str


def _default_service_user() -> str:
    return getpass.getuser()


def _service_units_for_user(target_user: str) -> tuple[tuple[str, str], ...]:
    user_text = normalize_text(target_user)
    if not user_text:
        raise ValueError("service user is required")
    return tuple((label, template.format(user=user_text)) for label, template in SERVICE_UNIT_ORDER)


def _single_line_command_output(completed: subprocess.CompletedProcess[Any]) -> str:
    output = normalize_text(completed.stdout)
    if not output:
        output = normalize_text(completed.stderr)
    if not output:
        output = f"exit {completed.returncode}"
    return output.splitlines()[0]


def _query_systemctl(
    action: str,
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["systemctl", action, unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return f"error: {exc}"
    return _single_line_command_output(completed)


def collect_service_status(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[ServiceUnitStatus, ...]:
    return tuple(
        ServiceUnitStatus(
            label=label,
            unit=unit,
            active=_query_systemctl("is-active", unit, run=run),
            enabled=_query_systemctl("is-enabled", unit, run=run),
        )
        for label, unit in _service_units_for_user(target_user)
    )


_SERVICE_ACTIVE_COLORS = {
    "active": cli_style.GREEN,
    "failed": cli_style.RED,
    "inactive": cli_style.DIM,
    "dead": cli_style.DIM,
}


def _service_active_color(value: str) -> str:
    return _SERVICE_ACTIVE_COLORS.get(value.strip().lower(), cli_style.YELLOW)


def _paint_field(text: str, width: int, color: str | None) -> str:
    padded = f"{text:<{width}}"
    return cli_style.paint(padded, color) if color else padded


def _print_service_status(target_user: str, statuses: Sequence[ServiceUnitStatus]) -> None:
    print(f"orca_auto service status for {target_user} ({_selected_service_mode(statuses)}):")
    print(cli_style.paint(f"{'Name':<10} {'Active':<14} Unit", cli_style.BOLD))
    for status in statuses:
        active = _paint_field(status.active, 14, _service_active_color(status.active))
        print(f"{status.label:<10} {active} {status.unit}")


def _service_status_payload(
    target_user: str, statuses: Sequence[ServiceUnitStatus]
) -> dict[str, Any]:
    mode = _selected_service_mode(statuses)
    required_labels = _required_service_labels(mode)
    return {
        "target_user": target_user,
        "mode": mode,
        "ok": _required_services_active(statuses, required_labels=required_labels),
        "services": [
            {
                "label": status.label,
                "unit": status.unit,
                "active": status.active,
                "enabled": status.enabled,
                "required": status.label in required_labels,
            }
            for status in statuses
        ],
    }


def _selected_service_mode(statuses: Sequence[ServiceUnitStatus]) -> str:
    by_label = {status.label: status for status in statuses}
    runtime = by_label.get("runtime")
    worker = by_label.get("worker")
    if runtime is not None and runtime.enabled == "enabled":
        return "full"
    if worker is not None and worker.enabled == "enabled":
        return "worker-only"
    # Fall back to the live graph when neither boot selection can be read.
    if runtime is not None and runtime.active == "active":
        return "full"
    return "worker-only"


def _required_service_labels(mode: str) -> frozenset[str]:
    if mode == "full":
        return frozenset({"runtime", "worker", "bot"})
    return frozenset({"worker"})


def _required_services_active(
    statuses: Sequence[ServiceUnitStatus], *, required_labels: frozenset[str]
) -> bool:
    by_label = {status.label: status for status in statuses}
    return all(
        label in by_label and by_label[label].active == "active" for label in required_labels
    )


def _systemctl_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("systemctl") is not None


def _sudo_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("sudo") is not None


def _runtime_unit_for_user(target_user: str) -> str:
    return f"orca_auto-runtime@{target_user}.target"


def _worker_unit_for_user(target_user: str) -> str:
    return f"orca_auto-queue-worker@{target_user}.service"


def _restart_unit_for_user(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    runtime_unit = _runtime_unit_for_user(target_user)
    runtime_active = _query_systemctl("is-active", runtime_unit, run=run)
    runtime_enabled = _query_systemctl("is-enabled", runtime_unit, run=run)
    if runtime_active == "active" or runtime_enabled == "enabled":
        return runtime_unit
    return _worker_unit_for_user(target_user)


@dataclass(frozen=True)
class ServiceCliDeps:
    """Optional overrides for system-effect seams (test injection)."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    which: Callable[[str], str | None] | None = None
    is_root: Callable[[], bool] | None = None
    default_service_user: Callable[[], str] | None = None
    collect_service_status: Callable[..., tuple[ServiceUnitStatus, ...]] | None = None
    restart_unit_for_user: Callable[..., str] | None = None


def _service_target_user(args: argparse.Namespace, deps: ServiceCliDeps) -> str:
    default_user = deps.default_service_user or _default_service_user
    return normalize_text(getattr(args, "target_user", None)) or normalize_text(default_user())


def cmd_service_status(args: argparse.Namespace, *, deps: ServiceCliDeps | None = None) -> int:
    deps = deps or ServiceCliDeps()
    which = deps.which or shutil.which
    collect_status = deps.collect_service_status or collect_service_status
    if not _systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1

    target_user = _service_target_user(args, deps)
    try:
        statuses = collect_status(target_user, run=deps.run or subprocess.run)
    except ValueError as exc:
        emit_error(exc)
        return 1
    payload = _service_status_payload(target_user, statuses)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        _print_service_status(target_user, statuses)
    return 0 if payload["ok"] else 1


def cmd_service_restart(args: argparse.Namespace, *, deps: ServiceCliDeps | None = None) -> int:
    deps = deps or ServiceCliDeps()
    which = deps.which or shutil.which
    run = deps.run or subprocess.run
    is_root = deps.is_root or _is_root
    restart_unit_for_user = deps.restart_unit_for_user or _restart_unit_for_user

    if not _systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1
    use_sudo = not is_root()
    if use_sudo and not _sudo_available(which=which):
        emit_error("sudo is required to restart system services; rerun as root")
        return 1

    target_user = _service_target_user(args, deps)
    try:
        unit = restart_unit_for_user(target_user, run=run)
    except ValueError as exc:
        emit_error(exc)
        return 1

    worker_unit = _worker_unit_for_user(target_user)
    print(f"Resetting worker failure state for {worker_unit}")
    rc = _run_command(
        ("systemctl", "reset-failed", worker_unit),
        use_sudo=use_sudo,
        run=run,
    )
    if rc != 0:
        return rc

    print(f"Restarting {unit}")
    rc = _run_command(("systemctl", "restart", unit), use_sudo=use_sudo, run=run)
    if rc == 0:
        print("Restart requested successfully.")
        print("Check status with: orca_auto service status")
    return rc


__all__ = [
    "SERVICE_UNIT_ORDER",
    "ServiceCliDeps",
    "ServiceUnitStatus",
    "cmd_service_restart",
    "cmd_service_status",
    "collect_service_status",
]
