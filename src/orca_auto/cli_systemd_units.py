from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto import systemd_plan
from orca_auto.core.utils.coercion import normalize_text

_SERVICE_UNIT_FACTORIES: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("runtime", systemd_plan._runtime_unit_for_user),
    ("engines", systemd_plan._engine_workers_unit_for_user),
    ("worker", systemd_plan._worker_unit_for_user),
    ("workflow", systemd_plan._workflow_worker_unit_for_user),
)
_ENABLED_UNIT_FILE_STATES = frozenset({"enabled", "enabled-runtime"})
_READABLE_UNIT_FILE_STATES = frozenset(
    {
        "alias",
        "disabled",
        "enabled",
        "enabled-runtime",
        "generated",
        "indirect",
        "linked",
        "linked-runtime",
        "masked",
        "masked-runtime",
        "static",
        "transient",
    }
)


@dataclass(frozen=True)
class ServiceUnitStatus:
    label: str
    unit: str
    active: str
    enabled: str


def _default_service_user() -> str:
    # These commands act on system units, so operators reach for `sudo
    # orca_auto service ...`. getpass.getuser() reports root under sudo, and
    # every unit name then resolves to an @root instance nobody installed.
    # systemd treats those as success rather than error -- `reset-failed`
    # exits 0 on a unit it reports as "not loaded" -- so the command claims to
    # have restarted workers it never touched. Template units cannot catch this
    # either: they load for any instance name. Prefer the invoking account.
    if systemd_plan._is_root():
        invoking_user = normalize_text(os.environ.get("SUDO_USER"))
        if invoking_user and invoking_user != "root":
            return invoking_user
    return getpass.getuser()


def _service_target_user(
    args: argparse.Namespace,
    *,
    default_service_user: Callable[[], str] | None = None,
) -> str:
    default_user = default_service_user or _default_service_user
    return normalize_text(getattr(args, "target_user", None)) or normalize_text(default_user())


def _service_units_for_user(target_user: str) -> tuple[tuple[str, str], ...]:
    user_text = normalize_text(target_user)
    if not user_text:
        raise ValueError("service user is required")
    return tuple(
        (label, unit_for_user(user_text)) for label, unit_for_user in _SERVICE_UNIT_FACTORIES
    )


def _single_line_command_output(completed: subprocess.CompletedProcess[Any]) -> str:
    output = normalize_text(completed.stdout)
    if not output:
        output = normalize_text(completed.stderr)
    if not output:
        output = f"exit {completed.returncode}"
    return output.splitlines()[0]


def _run_systemctl(
    action: str,
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    extra_args: Sequence[str] = (),
) -> subprocess.CompletedProcess[Any]:
    """Invoke systemctl for one unit; OSError stays with the caller's policy."""
    return run(
        ["systemctl", action, *extra_args, unit],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _show_unit_property(
    unit: str,
    property_name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    extra_args: Sequence[str] = (),
) -> subprocess.CompletedProcess[Any]:
    return _run_systemctl(
        "show",
        unit,
        run=run,
        extra_args=(f"--property={property_name}", "--value", *extra_args),
    )


def _query_systemctl(
    action: str,
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = _run_systemctl(action, unit, run=run)
    except OSError as exc:
        return f"error: {exc}"
    return _single_line_command_output(completed)


def _unit_load_state(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = _show_unit_property(unit, "LoadState", run=run)
    except OSError as exc:
        return f"error: {exc}"
    return _single_line_command_output(completed)


def _run_command(
    command: Sequence[str],
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    argv = systemd_plan._systemd_command_argv(command, use_sudo=use_sudo)
    print(f"$ {systemd_plan._format_command(command, use_sudo=use_sudo)}")
    completed = run(argv, check=False)
    return int(completed.returncode)


def _select_service_mode(
    *,
    enabled_state: Callable[[str], str | None],
    runtime_active: Callable[[], bool],
) -> str:
    """Boot-selection cascade shared by `service status` and `service restart`.

    ``enabled_state`` answers a role label with the unit's is-enabled state, or
    None when that unit was not observed. Roles are consulted lazily in cascade
    order, so callers that query systemctl per role issue no extra queries.
    """
    runtime = enabled_state("runtime")
    if runtime is not None and runtime in _ENABLED_UNIT_FILE_STATES:
        return "full"
    engines = enabled_state("engines")
    if engines is not None and engines in _ENABLED_UNIT_FILE_STATES:
        return "worker-only"
    # Recognize the previous direct worker boot selection as worker-only, but
    # health remains false until the new engine-worker target is installed.
    worker = enabled_state("worker")
    if worker is not None and worker in _ENABLED_UNIT_FILE_STATES:
        return "worker-only"
    observed = tuple(state for state in (runtime, engines, worker) if state is not None)
    # Fall back to the live graph only when the boot selection cannot be read.
    if (
        observed
        and not all(state in _READABLE_UNIT_FILE_STATES for state in observed)
        and runtime_active()
    ):
        return "full"
    return "worker-only"


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


def _systemctl_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("systemctl") is not None


__all__ = ["ServiceUnitStatus", "collect_service_status"]
