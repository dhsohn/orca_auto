from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from collections.abc import Callable
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


def _unit_load_state(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["systemctl", "show", "--property=LoadState", "--value", unit],
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


def _systemctl_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("systemctl") is not None


__all__ = ["ServiceUnitStatus", "collect_service_status"]
