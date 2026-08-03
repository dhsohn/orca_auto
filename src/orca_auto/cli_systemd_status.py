from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto import cli_style
from orca_auto._version import installed_version_drift
from orca_auto.cli_errors import emit_error
from orca_auto.cli_systemd_apply import _run_command
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.systemd_plan import _is_root

SERVICE_UNIT_ORDER = (
    ("runtime", "orca_auto-runtime@{user}.target"),
    ("engines", "orca_auto-engine-workers@{user}.target"),
    ("worker", "orca_auto-queue-worker@{user}.service"),
    ("workflow", "orca_auto-workflow-worker@{user}.service"),
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
    target_user: str,
    statuses: Sequence[ServiceUnitStatus],
    drift: tuple[str, str] | None = None,
    staleness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = _selected_service_mode(statuses)
    required_labels = _required_service_labels(mode)
    return {
        "target_user": target_user,
        "mode": mode,
        "ok": _required_services_active(statuses, required_labels=required_labels),
        "worker_staleness": staleness,
        "version_drift": (
            None
            if drift is None
            else {
                "installed": drift[0],
                "source": drift[1],
                # A host can hold several editable installs of one checkout —
                # here, the units' virtualenv and the operator's shell — so the
                # verdict is only meaningful with the interpreter it describes.
                "interpreter": sys.executable,
            }
        ),
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
    engines = by_label.get("engines")
    worker = by_label.get("worker")
    if runtime is not None and runtime.enabled in _ENABLED_UNIT_FILE_STATES:
        return "full"
    if engines is not None and engines.enabled in _ENABLED_UNIT_FILE_STATES:
        return "worker-only"
    # Recognize the previous direct worker boot selection as worker-only, but
    # health remains false until the new engine-worker target is installed.
    if worker is not None and worker.enabled in _ENABLED_UNIT_FILE_STATES:
        return "worker-only"
    enabled_states = tuple(
        status.enabled for status in (runtime, engines, worker) if status is not None
    )
    # Fall back to the live graph only when the boot selection cannot be read.
    if (
        enabled_states
        and not all(state in _READABLE_UNIT_FILE_STATES for state in enabled_states)
        and runtime is not None
        and runtime.active == "active"
    ):
        return "full"
    return "worker-only"


def _required_service_labels(mode: str) -> frozenset[str]:
    if mode == "full":
        return frozenset({"runtime", "engines", "worker"})
    return frozenset({"engines", "worker"})


def _required_services_active(
    statuses: Sequence[ServiceUnitStatus], *, required_labels: frozenset[str]
) -> bool:
    by_label = {status.label: status for status in statuses}
    return all(
        label in by_label and by_label[label].active == "active" for label in required_labels
    )


_WORKER_PROCESS_LABELS = frozenset({"worker", "workflow"})


def _unit_main_pid(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    try:
        completed = run(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return 0
    try:
        return int(_single_line_command_output(completed))
    except ValueError:
        return 0


def _unit_start_epoch(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> float:
    """Epoch at which the unit's main process started, from systemd's record.

    systemd snapshots CLOCK_REALTIME when it forks the main process, so the
    value stays true after later clock steps. Deriving the start from
    ``/proc/<pid>/stat`` ticks plus ``btime`` does not: ``btime`` is recomputed
    from the current wall clock minus the monotonic uptime, and on WSL2 the
    wall clock is stepped forward after host sleeps while the monotonic clock
    stood still, which shifts every derived start time forward and can mask a
    genuinely stale worker (observed live: +32 min).
    """
    try:
        completed = run(
            [
                "systemctl",
                "show",
                "--property=ExecMainStartTimestamp",
                "--value",
                "--timestamp=utc",
                unit,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ValueError(f"systemctl show failed: {exc}") from exc
    value = _single_line_command_output(completed)
    # "Mon 2026-08-03 09:33:12 UTC" — parse without the weekday token so the
    # verdict does not depend on the CLI locale.
    tokens = value.split()
    if len(tokens) != 4 or tokens[3] != "UTC":
        raise ValueError(f"unrecognized ExecMainStartTimestamp: {value!r}")
    started = datetime.strptime(f"{tokens[1]} {tokens[2]}", "%Y-%m-%d %H:%M:%S")
    return started.replace(tzinfo=UTC).timestamp()


def _head_commit_epoch(
    source_root: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    completed = run(
        ["git", "-C", str(source_root), "log", "-1", "--format=%ct"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(
            normalize_text(completed.stderr).splitlines()[0]
            if normalize_text(completed.stderr)
            else f"git log exited {completed.returncode}"
        )
    return int(_single_line_command_output(completed))


def _epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_worker_staleness(
    statuses: Sequence[ServiceUnitStatus],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    source_root: Path | None = None,
) -> dict[str, Any] | None:
    """Compare each active worker process against the checkout's HEAD commit.

    The units import the checkout live (editable install) but never reload, so
    a deploy that lands after a worker started leaves that process running a
    torn mix of cached pre-deploy modules and freshly imported post-deploy
    code. A worker whose systemd-recorded start predates HEAD is reported
    stale; an active worker whose start cannot be read is reported
    undetermined rather than assumed fresh. Returns ``None`` when the source
    tree is not a git checkout, which is the normal shape of a wheel install
    rather than a verdict — mirroring ``installed_version_drift``.
    """
    root = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return None
    try:
        head_epoch = _head_commit_epoch(root, run=run)
    except (OSError, ValueError, IndexError) as exc:
        # A .git directory with an unreadable history is an anomaly, not a
        # wheel install, so it cannot vouch for the workers.
        return {
            "head_commit_epoch": None,
            "stale": [],
            "undetermined": [
                {"label": "", "unit": "", "detail": f"cannot read HEAD commit: {exc}"}
            ],
        }
    stale: list[dict[str, Any]] = []
    undetermined: list[dict[str, Any]] = []
    for status in statuses:
        if status.label not in _WORKER_PROCESS_LABELS or status.active != "active":
            continue
        try:
            started_epoch = _unit_start_epoch(status.unit, run=run)
        except ValueError as exc:
            undetermined.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "detail": f"cannot read unit start time: {exc}",
                }
            )
            continue
        if started_epoch < head_epoch:
            stale.append(
                {
                    "label": status.label,
                    "unit": status.unit,
                    "pid": _unit_main_pid(status.unit, run=run),
                    "started_epoch": int(started_epoch),
                }
            )
    return {"head_commit_epoch": head_epoch, "stale": stale, "undetermined": undetermined}


def _systemctl_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("systemctl") is not None


def _sudo_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("sudo") is not None


def _runtime_unit_for_user(target_user: str) -> str:
    return f"orca_auto-runtime@{target_user}.target"


def _engine_workers_unit_for_user(target_user: str) -> str:
    return f"orca_auto-engine-workers@{target_user}.target"


def _worker_unit_for_user(target_user: str) -> str:
    return f"orca_auto-queue-worker@{target_user}.service"


def _require_current_restart_units(
    target_user: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    required_units = (_engine_workers_unit_for_user(target_user),)
    missing = tuple(
        unit for unit in required_units if _unit_load_state(unit, run=run) == "not-found"
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
    runtime_unit = _runtime_unit_for_user(target_user)
    engines_unit = _engine_workers_unit_for_user(target_user)
    worker_unit = _worker_unit_for_user(target_user)
    runtime_enabled = _query_systemctl("is-enabled", runtime_unit, run=run)
    if runtime_enabled in _ENABLED_UNIT_FILE_STATES:
        return runtime_unit
    engines_enabled = _query_systemctl("is-enabled", engines_unit, run=run)
    if engines_enabled in _ENABLED_UNIT_FILE_STATES:
        return engines_unit
    worker_enabled = _query_systemctl("is-enabled", worker_unit, run=run)
    if worker_enabled in _ENABLED_UNIT_FILE_STATES:
        return engines_unit
    if not all(
        state in _READABLE_UNIT_FILE_STATES
        for state in (runtime_enabled, engines_enabled, worker_enabled)
    ):
        runtime_active = _query_systemctl("is-active", runtime_unit, run=run)
        if runtime_active == "active":
            return runtime_unit
    return engines_unit


@dataclass(frozen=True)
class ServiceCliDeps:
    """Optional overrides for system-effect seams (test injection)."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    which: Callable[[str], str | None] | None = None
    is_root: Callable[[], bool] | None = None
    default_service_user: Callable[[], str] | None = None
    collect_service_status: Callable[..., tuple[ServiceUnitStatus, ...]] | None = None
    restart_unit_for_user: Callable[..., str] | None = None
    installed_version_drift: Callable[[], tuple[str, str] | None] | None = None
    collect_worker_staleness: Callable[..., dict[str, Any] | None] | None = None


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
    drift = (deps.installed_version_drift or installed_version_drift)()
    staleness = (deps.collect_worker_staleness or collect_worker_staleness)(
        statuses, run=deps.run or subprocess.run
    )
    payload = _service_status_payload(target_user, statuses, drift, staleness)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        _print_service_status(target_user, statuses)
    if drift is not None:
        # This interpreter runs the checkout's code but declares the version its
        # last install froze, so every version it reports is wrong until the
        # editable install is refreshed. The verdict covers only the interpreter
        # that ran this command, which need not be the one the units run.
        installed, source = drift
        emit_error(
            f"{sys.executable} declares orca_auto {installed} but runs the source tree at {source}",
            hint=f"rerun `{sys.executable} -m pip install -e .`",
        )
    staleness_ok = staleness is None or not (staleness["stale"] or staleness["undetermined"])
    if staleness is not None:
        # Stale entries only exist once HEAD was read, so the epoch is present.
        head_epoch = float(staleness["head_commit_epoch"] or 0)
        for entry in staleness["stale"]:
            emit_error(
                f"{entry['unit']} (pid {entry['pid']}) started "
                f"{_epoch_iso(entry['started_epoch'])}, before the checkout's HEAD commit "
                f"{_epoch_iso(head_epoch)}; the process still runs pre-deploy code",
                hint="restart the workers in an idle window: orca_auto service restart",
            )
        for entry in staleness["undetermined"]:
            emit_error(
                "cannot judge worker code freshness"
                + (f" for {entry['unit']}" if entry["unit"] else "")
                + f": {entry['detail']}",
                hint="restart the workers in an idle window: orca_auto service restart",
            )
    return 0 if payload["ok"] and drift is None and staleness_ok else 1


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

    reset_units = [
        _worker_unit_for_user(target_user),
    ]
    for reset_unit in reset_units:
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
    "collect_worker_staleness",
]
