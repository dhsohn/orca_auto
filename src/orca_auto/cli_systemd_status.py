from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orca_auto import cli_systemd_freshness, cli_systemd_units
from orca_auto.core import terminal
from orca_auto.core.terminal import emit_error
from orca_auto.core.utils.coercion import normalize_text

_SERVICE_ACTIVE_COLORS = {
    "active": terminal.GREEN,
    "failed": terminal.RED,
    "inactive": terminal.DIM,
    "dead": terminal.DIM,
}


def _service_active_color(value: str) -> str:
    return _SERVICE_ACTIVE_COLORS.get(value.strip().lower(), terminal.YELLOW)


def _paint_field(text: str, width: int, color: str | None) -> str:
    padded = f"{text:<{width}}"
    return terminal.paint(padded, color) if color else padded


def _print_service_status(
    target_user: str, statuses: Sequence[cli_systemd_units.ServiceUnitStatus]
) -> None:
    print(f"orca_auto service status for {target_user} ({_selected_service_mode(statuses)}):")
    print(terminal.paint(f"{'Name':<10} {'Active':<14} Unit", terminal.BOLD))
    for status in statuses:
        active = _paint_field(status.active, 14, _service_active_color(status.active))
        print(f"{status.label:<10} {active} {status.unit}")


def _service_status_payload(
    target_user: str,
    statuses: Sequence[cli_systemd_units.ServiceUnitStatus],
    staleness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = _selected_service_mode(statuses)
    required_labels = _required_service_labels(mode)
    return {
        "target_user": target_user,
        "mode": mode,
        "ok": _required_services_active(statuses, required_labels=required_labels),
        "worker_staleness": staleness,
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


def _selected_service_mode(statuses: Sequence[cli_systemd_units.ServiceUnitStatus]) -> str:
    by_label = {status.label: status for status in statuses}
    runtime = by_label.get("runtime")

    def enabled_state(label: str) -> str | None:
        status = by_label.get(label)
        return None if status is None else status.enabled

    return cli_systemd_units.select_service_mode(
        enabled_state=enabled_state,
        runtime_active=lambda: runtime is not None and runtime.active == "active",
    )


def _required_service_labels(mode: str) -> frozenset[str]:
    if mode == "full":
        return frozenset({"runtime", "engines", "worker"})
    return frozenset({"engines", "worker"})


def _required_services_active(
    statuses: Sequence[cli_systemd_units.ServiceUnitStatus], *, required_labels: frozenset[str]
) -> bool:
    by_label = {status.label: status for status in statuses}
    return all(
        label in by_label and by_label[label].active == "active" for label in required_labels
    )


def _epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ServiceStatusDeps:
    """Optional overrides for service-status system effects."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    which: Callable[[str], str | None] | None = None
    default_service_user: Callable[[], str] | None = None
    collect_service_status: (
        Callable[..., tuple[cli_systemd_units.ServiceUnitStatus, ...]] | None
    ) = None
    collect_worker_staleness: Callable[..., dict[str, Any] | None] | None = None


def cmd_service_status(args: argparse.Namespace, *, deps: ServiceStatusDeps | None = None) -> int:
    deps = deps or ServiceStatusDeps()
    which = deps.which or shutil.which
    collect_status = deps.collect_service_status or cli_systemd_units.collect_service_status
    if not cli_systemd_units.systemctl_available(which=which):
        emit_error("systemctl is not available in this environment")
        return 1

    target_user = cli_systemd_units.service_target_user(
        args, default_user=deps.default_service_user
    )
    try:
        statuses = collect_status(target_user, run=deps.run or subprocess.run)
    except ValueError as exc:
        emit_error(exc)
        return 1
    staleness = (deps.collect_worker_staleness or cli_systemd_freshness.collect_worker_staleness)(
        statuses, run=deps.run or subprocess.run
    )
    payload = _service_status_payload(target_user, statuses, staleness)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        _print_service_status(target_user, statuses)
        for entry in (staleness or {}).get("workers", []):
            if entry.get("runtime_build_id"):
                print(
                    f"runtime_build: {entry['unit']} {entry['runtime_build_id']} ({entry['source_root']})"
                )
    staleness_ok = staleness is None or not (staleness["stale"] or staleness["undetermined"])
    if staleness is not None:
        for entry in staleness["stale"]:
            if entry.get("expected_runtime_build_id"):
                emit_error(
                    f"{entry['unit']} (pid {entry['pid']}) runs "
                    f"{entry.get('runtime_build_id') or entry.get('source_root', 'another source')}; "
                    f"installed unit requires runtime {entry['expected_runtime_build_id']} "
                    f"in {entry['expected_runtime_root']}",
                    hint="restart the workers in an idle window: orca_auto service restart",
                )
                continue
            # The collector dates every stale checkout worker by its own
            # checkout's HEAD update. An entry without that field cannot be
            # explained as a deploy-time comparison; say so instead of
            # rendering a 1970 timestamp.
            head_update_epoch = entry.get("head_update_epoch")
            if head_update_epoch is None:
                emit_error(
                    f"cannot judge worker code freshness for {entry['unit']}: "
                    "stale verdict carries no checkout update time",
                    hint="restart the workers in an idle window: orca_auto service restart",
                )
                continue
            source_detail = (
                f" in {entry['source_root']}" if normalize_text(entry.get("source_root")) else ""
            )
            sha_detail = (
                f" ({normalize_text(entry.get('head_sha'))[:12]})"
                if normalize_text(entry.get("head_sha"))
                else ""
            )
            emit_error(
                f"{entry['unit']} (pid {entry['pid']}) started "
                f"{_epoch_iso(entry['started_epoch'])}, before checkout HEAD{sha_detail}{source_detail} "
                f"was updated {_epoch_iso(float(head_update_epoch))}; the process still runs "
                "pre-deploy code",
                hint="restart the workers in an idle window: orca_auto service restart",
            )
        for entry in staleness["undetermined"]:
            emit_error(
                "cannot judge worker code freshness"
                + (f" for {entry['unit']}" if entry["unit"] else "")
                + f": {entry['detail']}",
                hint="restart the workers in an idle window: orca_auto service restart",
            )
    return 0 if payload["ok"] and staleness_ok else 1


__all__ = ["ServiceStatusDeps", "cmd_service_status"]
