from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_errors import emit_error
from orca_auto.core.utils import process as process_utils
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

_TRANSACTION_DIR_NAME = ".orca_auto-install-transaction"
_TRANSACTION_MANIFEST_NAME = "manifest.json"
_TRANSACTION_OWNER_NAME = "owner.json"
_UNIT_PHASE_PREPARED = "prepared"
_UNIT_PHASE_BACKUPS_DURABLE = "backups_durable"
_UNIT_PHASE_PUBLISHED = "units_published"
_RUNTIME_PHASE_NOT_STARTED = "not_started"
_RUNTIME_PHASE_RESTART_PENDING = "restart_pending"
_RUNTIME_PHASE_RESTART_SUCCEEDED = "restart_succeeded"
_RUNTIME_PHASE_RESTART_FAILED = "restart_failed"
_PENDING_RESTART_APPLIED = "applied"
_PENDING_RESTART_NOT_APPLIED = "not-applied"
_PENDING_RESTART_RESOLUTIONS = frozenset({_PENDING_RESTART_APPLIED, _PENDING_RESTART_NOT_APPLIED})
_VALID_UNIT_PHASES = frozenset(
    {_UNIT_PHASE_PREPARED, _UNIT_PHASE_BACKUPS_DURABLE, _UNIT_PHASE_PUBLISHED}
)
_VALID_RUNTIME_PHASES = frozenset(
    {
        _RUNTIME_PHASE_NOT_STARTED,
        _RUNTIME_PHASE_RESTART_PENDING,
        _RUNTIME_PHASE_RESTART_SUCCEEDED,
        _RUNTIME_PHASE_RESTART_FAILED,
    }
)
# Append-only: recovery must continue to accept unit names emitted by older
# manifest-v1 installers even after those units stop being rendered.
_RECOVERABLE_SYSTEMD_UNIT_NAMES_V1 = frozenset(
    {
        "orca_auto-engine-workers@.target",
        "orca_auto-queue-worker@.service",
        "orca_auto-xtb-md-worker@.service",
        "orca_auto-workflow-worker@.service",
        "orca_auto-bot@.service",
        "orca_auto-runtime@.target",
    }
)
# Append-only, for the same reason: an older manifest-v1 installer could record
# a retired unit in its active-component snapshot. Recovery accepts those names
# so a legacy manifest stays rollback-able; the retired units are never started,
# stopped, or verified, because they are absent from the current unit set.
_RECOVERABLE_ACTIVE_COMPONENT_TEMPLATES_V1 = (
    "orca_auto-xtb-md-worker@{user}.service",
    "orca_auto-bot@{user}.service",
)


@dataclass(frozen=True)
class _UnitFileBackup:
    name: str
    destination: Path
    backup: Path
    existed: bool
    original_identity: tuple[int, int, int, int, int, int] | None
    staged_identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class _BootSelection:
    unit: str
    state: str


@dataclass(frozen=True)
class _CommitResult:
    published: bool
    cleaned: bool


@dataclass(frozen=True)
class _TransactionSnapshot:
    backups: tuple[_UnitFileBackup, ...]
    boot: tuple[_BootSelection, ...]
    active_components: tuple[str, ...] | None
    unit_phase: str
    runtime_phase: str
    started_owner: str | None


class _TransactionBusyError(OSError):
    pass


def _boot_id() -> str | None:
    return process_utils.linux_boot_id(proc_root=Path("/proc"))


def _write_owner_file(
    plan: SystemdInstallPlan,
    local_dir: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    process_start_ticks = process_utils.process_start_ticks(os.getpid(), proc_root=Path("/proc"))
    boot_id = _boot_id()
    if process_start_ticks is None or not boot_id:
        raise OSError("could not identify the systemd installer process")
    local_owner = local_dir / _TRANSACTION_OWNER_NAME
    local_owner.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "boot_id": boot_id,
                "process_start_ticks": process_start_ticks,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    owner = _transaction_dir(plan) / _TRANSACTION_OWNER_NAME
    if plan.use_sudo:
        return _run_command(
            ("install", "-m", "0644", str(local_owner), str(owner)),
            use_sudo=True,
            run=run,
        )
    shutil.copy2(local_owner, owner)
    with owner.open("rb") as handle:
        os.fsync(handle.fileno())
    return 0


def _transaction_owner_is_live(transaction_dir: Path) -> bool | None:
    owner = transaction_dir / _TRANSACTION_OWNER_NAME
    if not owner.is_file():
        return None
    try:
        payload = json.loads(owner.read_text(encoding="utf-8"))
        pid = payload.get("pid")
        boot_id = payload.get("boot_id")
        process_start_ticks = payload.get("process_start_ticks")
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(pid, int) or pid <= 0 or not isinstance(boot_id, str) or not boot_id:
        return None
    current_boot_id = _boot_id()
    if not current_boot_id:
        return None
    if boot_id != current_boot_id:
        return False
    if (
        process_start_ticks is None
        or isinstance(process_start_ticks, bool)
        or not isinstance(process_start_ticks, int)
        or process_start_ticks <= 0
    ):
        return None
    observed_start_ticks = process_utils.process_start_ticks(pid, proc_root=Path("/proc"))
    if observed_start_ticks is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return None
    return observed_start_ticks == process_start_ticks


def _active_component_candidates(plan: SystemdInstallPlan) -> tuple[str, ...]:
    user = plan.target_user
    return (
        f"orca_auto-runtime@{user}.target",
        f"orca_auto-engine-workers@{user}.target",
        f"orca_auto-queue-worker@{user}.service",
    )


def _recoverable_active_components(plan: SystemdInstallPlan) -> frozenset[str]:
    user = plan.target_user
    return frozenset(_active_component_candidates(plan)).union(
        template.format(user=user) for template in _RECOVERABLE_ACTIVE_COMPONENT_TEMPLATES_V1
    )


def _restart_owner_for_command(plan: SystemdInstallPlan, command: Sequence[str]) -> str | None:
    if (
        len(command) == 3
        and tuple(command[:2]) == ("systemctl", "restart")
        and command[-1] in _active_component_candidates(plan)[:2]
    ):
        return command[-1]
    return None


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


def _transaction_dir(plan: SystemdInstallPlan) -> Path:
    return plan.unit_dir / _TRANSACTION_DIR_NAME


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _path_identity(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        details = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_nlink),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _identity_payload(
    identity: tuple[int, int, int, int, int, int] | None,
) -> dict[str, int] | None:
    if identity is None:
        return None
    keys = ("device", "inode", "mode", "links", "size", "mtime_ns")
    return dict(zip(keys, identity, strict=True))


def _identity_from_payload(value: object) -> tuple[int, int, int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    keys = ("device", "inode", "mode", "links", "size", "mtime_ns")
    items: list[int] = []
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        items.append(item)
    identity = (
        items[0],
        items[1],
        items[2],
        items[3],
        items[4],
        items[5],
    )
    if identity[0] < 0 or identity[1] <= 0 or identity[2] <= 0 or identity[3] <= 0:
        return None
    return identity


def _fsync_directories(paths: Sequence[Path]) -> None:
    for path in paths:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _boot_units_for_plan(plan: SystemdInstallPlan) -> tuple[str, ...]:
    units: dict[str, None] = {}
    for command in plan.commands:
        if len(command) >= 3 and command[:2] in {
            ("systemctl", "enable"),
            ("systemctl", "disable"),
        }:
            units.setdefault(command[-1], None)
    return tuple(units)


def _snapshot_boot_selections(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[_BootSelection, ...]:
    snapshots: list[_BootSelection] = []
    supported = {
        "disabled",
        "enabled",
        "enabled-runtime",
        "masked",
        "masked-runtime",
        "not-found",
    }
    for unit in _boot_units_for_plan(plan):
        command = ("systemctl", "is-enabled", unit)
        argv = _systemd_command_argv(command, use_sudo=plan.use_sudo)
        print(f"$ {_format_command(command, use_sudo=plan.use_sudo)}")
        completed = run(argv, check=False, capture_output=True, text=True)
        output = str(completed.stdout or completed.stderr or "").strip().splitlines()
        state = output[0] if output else ""
        if state not in supported:
            raise OSError(f"could not snapshot enable state for {unit}: {state or 'unknown'}")
        snapshots.append(_BootSelection(unit=unit, state=state))
    return tuple(snapshots)


def _systemd_active_state(
    plan: SystemdInstallPlan,
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    command = ("systemctl", "show", "--property=ActiveState", "--value", unit)
    argv = _systemd_command_argv(command, use_sudo=plan.use_sudo)
    print(f"$ {_format_command(command, use_sudo=plan.use_sudo)}")
    completed = run(argv, check=False, capture_output=True, text=True)
    state = str(completed.stdout or "").strip()
    if completed.returncode == 0 and state == "failed":
        return "inactive"
    if completed.returncode != 0 or state not in {"active", "inactive"}:
        detail = state or str(completed.stderr or "").strip() or f"exit {completed.returncode}"
        raise OSError(f"could not snapshot active state for {unit}: {detail}")
    return state


def _snapshot_active_components(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[str, ...] | None:
    if not any(command[:2] == ("systemctl", "restart") for command in plan.commands):
        return None
    active: list[str] = []
    for unit in _active_component_candidates(plan):
        if _systemd_active_state(plan, unit, run=run) == "active":
            active.append(unit)
    return tuple(active)


def _publish_manifest_payload(
    plan: SystemdInstallPlan,
    payload: dict[str, Any],
    local_dir: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    transaction_dir = _transaction_dir(plan)
    local_manifest = local_dir / _TRANSACTION_MANIFEST_NAME
    with local_manifest.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    manifest_tmp = transaction_dir / f"{_TRANSACTION_MANIFEST_NAME}.tmp"
    manifest = transaction_dir / _TRANSACTION_MANIFEST_NAME
    if plan.use_sudo:
        rc = _run_command(
            ("install", "-m", "0644", str(local_manifest), str(manifest_tmp)),
            use_sudo=True,
            run=run,
        )
        if rc != 0:
            return rc
        rc = _run_command(("mv", "--", str(manifest_tmp), str(manifest)), use_sudo=True, run=run)
        if rc != 0:
            return rc
    else:
        shutil.copy2(local_manifest, manifest_tmp)
        with manifest_tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(manifest_tmp, manifest)
    _fsync_directories((transaction_dir,))
    if not manifest.is_file():
        emit_error(f"systemd transaction manifest was not published: {manifest}")
        return 1
    if plan.use_sudo:
        # The sudo install/mv left the manifest bytes in cache; the non-sudo path
        # already fsynced the temp file before the rename. Flush just this file so
        # recovery can read the published manifest back.
        with manifest.open("rb") as handle:
            os.fsync(handle.fileno())
    return 0


def _write_manifest(
    plan: SystemdInstallPlan,
    backups: Sequence[_UnitFileBackup],
    boot: Sequence[_BootSelection],
    active_components: Sequence[str] | None,
    local_dir: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    payload: dict[str, Any] = {
        "version": 1,
        "target_user": plan.target_user,
        "unit_dir": str(plan.unit_dir),
        "unit_phase": _UNIT_PHASE_PREPARED,
        "runtime_phase": _RUNTIME_PHASE_NOT_STARTED,
        "started_owner": None,
        "units": [
            {
                "name": item.name,
                "destination": str(item.destination),
                "existed": item.existed,
                "original_identity": _identity_payload(item.original_identity),
                "staged_identity": _identity_payload(item.staged_identity),
            }
            for item in backups
        ],
        "boot": [{"unit": item.unit, "state": item.state} for item in boot],
        "active_components": (None if active_components is None else list(active_components)),
    }
    return _publish_manifest_payload(plan, payload, local_dir, run=run)


def _read_manifest_payload(plan: SystemdInstallPlan) -> dict[str, Any]:
    manifest = _transaction_dir(plan) / _TRANSACTION_MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise OSError(f"could not read pending systemd transaction {manifest}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("target_user") != plan.target_user
        or payload.get("unit_dir") != str(plan.unit_dir)
    ):
        raise OSError(f"pending systemd transaction does not match this install: {manifest}")
    return payload


def _update_manifest_fields(
    plan: SystemdInstallPlan,
    updates: dict[str, Any],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    payload = _read_manifest_payload(plan)
    payload.update(updates)
    with tempfile.TemporaryDirectory(prefix="orca_auto-systemd-manifest-") as local_dir_text:
        return _publish_manifest_payload(plan, payload, Path(local_dir_text), run=run)


def _remove_transaction(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    transaction_dir = _transaction_dir(plan)
    if transaction_dir.name != _TRANSACTION_DIR_NAME or transaction_dir.parent != plan.unit_dir:
        emit_error(f"refusing to remove unexpected transaction path: {transaction_dir}")
        return False
    if not _path_exists(transaction_dir):
        return True
    if plan.use_sudo:
        return (
            _run_command(
                ("rm", "-rf", "--", str(transaction_dir)),
                use_sudo=True,
                run=run,
            )
            == 0
        )
    try:
        shutil.rmtree(transaction_dir)
    except OSError as exc:
        emit_error(f"failed to remove systemd transaction {transaction_dir}: {exc}")
        return False
    return True


def _restore_unit_files(
    backups: Sequence[_UnitFileBackup],
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    restored = True
    for item in reversed(backups):
        try:
            backup_identity = _path_identity(item.backup)
            destination_identity = _path_identity(item.destination)
            if item.existed and item.original_identity is None:
                emit_error(f"missing original systemd unit identity: {item.destination}")
                restored = False
                continue
            if not item.existed and item.original_identity is not None:
                emit_error(f"unexpected original systemd unit identity: {item.destination}")
                restored = False
                continue
            if item.existed:
                if backup_identity is None:
                    if destination_identity != item.original_identity:
                        emit_error(
                            f"missing or ambiguous original systemd unit backup: {item.backup}"
                        )
                        restored = False
                    continue
                if backup_identity != item.original_identity:
                    emit_error(f"original systemd unit backup identity changed: {item.backup}")
                    restored = False
                    continue
                if destination_identity not in {None, item.staged_identity}:
                    emit_error(
                        f"systemd unit destination changed during rollback: {item.destination}"
                    )
                    restored = False
                    continue
            else:
                if backup_identity is not None:
                    emit_error(f"unexpected systemd unit backup: {item.backup}")
                    restored = False
                    continue
                if destination_identity not in {None, item.staged_identity}:
                    emit_error(
                        f"systemd unit destination appeared during rollback: {item.destination}"
                    )
                    restored = False
                    continue
            if use_sudo:
                rc = _run_command(
                    ("rm", "-f", "--", str(item.destination)),
                    use_sudo=True,
                    run=run,
                )
                if rc == 0 and item.existed:
                    rc = _run_command(
                        ("mv", "--", str(item.backup), str(item.destination)),
                        use_sudo=True,
                        run=run,
                    )
                restored = restored and rc == 0
            else:
                item.destination.unlink(missing_ok=True)
                if item.existed:
                    os.replace(item.backup, item.destination)
        except OSError as exc:
            emit_error(f"failed to restore systemd unit {item.destination}: {exc}")
            restored = False
    if restored:
        try:
            backup_dirs = {item.backup.parent for item in backups}
            unit_dirs = {item.destination.parent for item in backups}
            _fsync_directories(tuple((*backup_dirs, *unit_dirs)))
        except OSError as exc:
            emit_error(f"failed to sync restored systemd unit directories: {exc}")
            restored = False
    return restored


def _restore_boot_selections(
    snapshots: Sequence[_BootSelection],
    *,
    before_files: bool,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    restored = True
    for item in reversed(snapshots):
        if (item.state == "not-found") is not before_files:
            continue
        if item.state == "enabled-runtime":
            # A failed install may have created a persistent enable symlink.
            # Remove it before recreating the exact runtime-only selection.
            rc = _run_command(("systemctl", "disable", item.unit), use_sudo=use_sudo, run=run)
            if rc == 0:
                rc = _run_command(
                    ("systemctl", "enable", "--runtime", item.unit),
                    use_sudo=use_sudo,
                    run=run,
                )
        elif item.state == "enabled":
            rc = _run_command(("systemctl", "enable", item.unit), use_sudo=use_sudo, run=run)
        elif item.state in {"masked", "masked-runtime"}:
            rc = _run_command(("systemctl", "disable", item.unit), use_sudo=use_sudo, run=run)
            if rc == 0:
                command = ["systemctl", "mask"]
                if item.state.endswith("-runtime"):
                    command.append("--runtime")
                command.append(item.unit)
                rc = _run_command(tuple(command), use_sudo=use_sudo, run=run)
        else:
            rc = _run_command(("systemctl", "disable", item.unit), use_sudo=use_sudo, run=run)
        restored = restored and rc == 0
    return restored


def _reload_after_rollback(
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    rc = _run_command(("systemctl", "daemon-reload"), use_sudo=use_sudo, run=run)
    if rc != 0:
        emit_error("systemd unit files were restored but daemon-reload failed")
        return False
    return True


def _load_transaction(
    plan: SystemdInstallPlan,
) -> _TransactionSnapshot:
    transaction_dir = _transaction_dir(plan)
    manifest = transaction_dir / _TRANSACTION_MANIFEST_NAME
    payload = _read_manifest_payload(plan)
    unit_phase = payload.get("unit_phase")
    runtime_phase = payload.get("runtime_phase")
    if not isinstance(unit_phase, str) or unit_phase not in _VALID_UNIT_PHASES:
        raise OSError(f"pending systemd transaction has an invalid unit phase: {manifest}")
    if not isinstance(runtime_phase, str) or runtime_phase not in _VALID_RUNTIME_PHASES:
        raise OSError(f"pending systemd transaction has an invalid runtime phase: {manifest}")
    unit_entries = payload.get("units")
    if not isinstance(unit_entries, list) or len(unit_entries) > len(
        _RECOVERABLE_SYSTEMD_UNIT_NAMES_V1
    ):
        raise OSError(f"pending systemd transaction has an invalid unit list: {manifest}")
    backups: list[_UnitFileBackup] = []
    seen: set[str] = set()
    for entry in unit_entries:
        if not isinstance(entry, dict):
            raise OSError(f"pending systemd transaction has an invalid unit entry: {manifest}")
        name = entry.get("name")
        destination_text = entry.get("destination")
        existed = entry.get("existed")
        raw_original_identity = entry.get("original_identity")
        original_identity = _identity_from_payload(raw_original_identity)
        staged_identity = _identity_from_payload(entry.get("staged_identity"))
        if (
            not isinstance(name, str)
            or name in seen
            or name not in _RECOVERABLE_SYSTEMD_UNIT_NAMES_V1
            or destination_text != str(plan.unit_dir / name)
            or not isinstance(existed, bool)
            or (existed and original_identity is None)
            or (not existed and raw_original_identity is not None)
            or staged_identity is None
        ):
            raise OSError(f"pending systemd transaction has an unsafe unit entry: {manifest}")
        seen.add(name)
        backups.append(
            _UnitFileBackup(
                name=name,
                destination=plan.unit_dir / name,
                backup=transaction_dir / "backup" / name,
                existed=existed,
                original_identity=original_identity,
                staged_identity=staged_identity,
            )
        )
    boot_entries = payload.get("boot")
    safe_boot_units = {
        f"orca_auto-runtime@{plan.target_user}.target",
        f"orca_auto-engine-workers@{plan.target_user}.target",
        f"orca_auto-queue-worker@{plan.target_user}.service",
    }
    safe_states = {
        "disabled",
        "enabled",
        "enabled-runtime",
        "masked",
        "masked-runtime",
        "not-found",
    }
    if not isinstance(boot_entries, list):
        raise OSError(f"pending systemd transaction has an invalid boot snapshot: {manifest}")
    boot: list[_BootSelection] = []
    seen_boot: set[str] = set()
    for entry in boot_entries:
        if not isinstance(entry, dict):
            raise OSError(f"pending systemd transaction has an invalid boot entry: {manifest}")
        unit = entry.get("unit")
        state = entry.get("state")
        if (
            not isinstance(unit, str)
            or unit in seen_boot
            or unit not in safe_boot_units
            or not isinstance(state, str)
            or state not in safe_states
        ):
            raise OSError(f"pending systemd transaction has an unsafe boot entry: {manifest}")
        seen_boot.add(unit)
        boot.append(_BootSelection(unit=unit, state=state))
    safe_active_components = _active_component_candidates(plan)
    recoverable_active_components = _recoverable_active_components(plan)
    raw_active_components = payload.get("active_components")
    if raw_active_components is not None and not isinstance(raw_active_components, list):
        raise OSError(f"pending systemd transaction has invalid active components: {manifest}")
    active_components_list: list[str] = []
    seen_active: set[str] = set()
    for unit in raw_active_components or ():
        if (
            not isinstance(unit, str)
            or unit in seen_active
            or unit not in recoverable_active_components
        ):
            raise OSError(f"pending systemd transaction has unsafe active components: {manifest}")
        seen_active.add(unit)
        active_components_list.append(unit)
    active_components = None if raw_active_components is None else tuple(active_components_list)
    started_owner = payload.get("started_owner")
    safe_started_owners = safe_active_components[:2]
    if started_owner is not None and (
        not isinstance(started_owner, str) or started_owner not in safe_started_owners
    ):
        raise OSError(f"pending systemd transaction has an unsafe started owner: {manifest}")
    if (runtime_phase == _RUNTIME_PHASE_NOT_STARTED) != (started_owner is None):
        raise OSError(f"pending systemd transaction has inconsistent runtime phase: {manifest}")
    if runtime_phase != _RUNTIME_PHASE_NOT_STARTED and (
        active_components is None or unit_phase != _UNIT_PHASE_PUBLISHED
    ):
        raise OSError(f"pending systemd transaction has inconsistent runtime state: {manifest}")
    return _TransactionSnapshot(
        backups=tuple(backups),
        boot=tuple(boot),
        active_components=active_components,
        unit_phase=unit_phase,
        runtime_phase=runtime_phase,
        started_owner=started_owner,
    )


def _stop_installer_started_owner(
    plan: SystemdInstallPlan,
    snapshot: _TransactionSnapshot,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    if snapshot.runtime_phase == _RUNTIME_PHASE_RESTART_PENDING:
        raise OSError(
            "pending systemd transaction stopped at an ambiguous restart boundary; "
            "preserving recovery data without stopping any service"
        )
    if snapshot.runtime_phase == _RUNTIME_PHASE_NOT_STARTED:
        return True
    if snapshot.started_owner is None or snapshot.active_components is None:
        raise OSError("pending systemd transaction has incomplete restart state")
    if snapshot.started_owner in snapshot.active_components:
        return True
    return (
        _run_command(
            ("systemctl", "stop", snapshot.started_owner),
            use_sudo=plan.use_sudo,
            run=run,
        )
        == 0
    )


def _verify_active_components(
    plan: SystemdInstallPlan,
    active_components: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    expected = set(active_components)
    for unit in _active_component_candidates(plan):
        try:
            state = _systemd_active_state(plan, unit, run=run)
        except OSError as exc:
            emit_error(exc)
            return False
        if (state == "active") != (unit in expected):
            emit_error(
                f"systemd rollback active state mismatch for {unit}: "
                f"expected {'active' if unit in expected else 'inactive'}, observed {state}"
            )
            return False
    return True


def _restore_previous_active_components(
    plan: SystemdInstallPlan,
    snapshot: _TransactionSnapshot,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    active_components = snapshot.active_components
    if active_components is None:
        return snapshot.runtime_phase == _RUNTIME_PHASE_NOT_STARTED
    if snapshot.runtime_phase == _RUNTIME_PHASE_NOT_STARTED:
        return _verify_active_components(plan, active_components, run=run)
    if snapshot.runtime_phase == _RUNTIME_PHASE_RESTART_PENDING:
        raise OSError("pending systemd transaction stopped at an ambiguous restart boundary")

    candidates = _active_component_candidates(plan)
    targets = candidates[:2]
    leaves = candidates[2:]
    expected = set(active_components)

    # Stop pre-install inactive targets first so a previous runtime selection
    # cannot keep newly started descendants alive. Starting an expected parent
    # may pull in an unexpected child, so stop that child again afterwards.
    for unit in targets:
        if unit not in expected and (
            _run_command(("systemctl", "stop", unit), use_sudo=plan.use_sudo, run=run) != 0
        ):
            return False
    for unit in targets:
        if unit not in expected:
            continue
        if (
            _run_command(
                ("systemctl", "reset-failed", unit),
                use_sudo=plan.use_sudo,
                run=run,
            )
            != 0
            or _run_command(
                ("systemctl", "restart", unit),
                use_sudo=plan.use_sudo,
                run=run,
            )
            != 0
        ):
            return False
    runtime, engine = targets
    if (
        runtime in expected
        and engine not in expected
        and (_run_command(("systemctl", "stop", engine), use_sudo=plan.use_sudo, run=run) != 0)
    ):
        return False
    for unit in leaves:
        if unit not in expected and (
            _run_command(("systemctl", "stop", unit), use_sudo=plan.use_sudo, run=run) != 0
        ):
            return False
    for unit in leaves:
        if unit not in expected:
            continue
        if (
            _run_command(
                ("systemctl", "reset-failed", unit),
                use_sudo=plan.use_sudo,
                run=run,
            )
            != 0
            or _run_command(
                ("systemctl", "restart", unit),
                use_sudo=plan.use_sudo,
                run=run,
            )
            != 0
        ):
            return False
    return _verify_active_components(plan, active_components, run=run)


def _rollback_pending_transaction(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    snapshot = _load_transaction(plan)
    new_owner_stopped = _stop_installer_started_owner(plan, snapshot, run=run)
    # Clean up a first-install not-found selection while the new template still
    # exists. Other states are restored after the original files and daemon view
    # are back; in particular, masking a still-present new regular file can fail.
    pre_file_boot_restored = (
        _restore_boot_selections(
            snapshot.boot,
            before_files=True,
            use_sudo=plan.use_sudo,
            run=run,
        )
        if new_owner_stopped
        else False
    )
    files_restored = (
        _restore_unit_files(snapshot.backups, use_sudo=plan.use_sudo, run=run)
        if pre_file_boot_restored
        else False
    )
    reloaded = _reload_after_rollback(use_sudo=plan.use_sudo, run=run) if files_restored else False
    boot_restored = (
        _restore_boot_selections(
            snapshot.boot,
            before_files=False,
            use_sudo=plan.use_sudo,
            run=run,
        )
        if reloaded
        else False
    )
    runtime_restored = (
        _restore_previous_active_components(plan, snapshot, run=run)
        if files_restored and boot_restored and reloaded
        else False
    )
    if not (
        pre_file_boot_restored
        and new_owner_stopped
        and files_restored
        and boot_restored
        and reloaded
        and runtime_restored
    ):
        emit_error(
            f"systemd rollback is incomplete; preserved recovery data at {_transaction_dir(plan)}"
        )
        return False
    if not _remove_transaction(plan, run=run):
        emit_error(
            f"systemd rollback completed but transaction cleanup failed: {_transaction_dir(plan)}"
        )
        return False
    return True


def _resolve_pending_restart(
    plan: SystemdInstallPlan,
    resolution: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    snapshot = _load_transaction(plan)
    if snapshot.runtime_phase != _RUNTIME_PHASE_RESTART_PENDING:
        emit_error("--resolve-pending-restart requires a transaction in restart_pending phase")
        return False
    updates: dict[str, Any]
    if resolution == _PENDING_RESTART_APPLIED:
        updates = {"runtime_phase": _RUNTIME_PHASE_RESTART_SUCCEEDED}
    else:
        updates = {
            "runtime_phase": _RUNTIME_PHASE_NOT_STARTED,
            "started_owner": None,
        }
    rc = _update_manifest_fields(plan, updates, run=run)
    if rc != 0:
        emit_error(
            "could not record the operator's pending-restart resolution; "
            f"preserved recovery data at {_transaction_dir(plan)}"
        )
        return False
    return True


def _recover_pending_transaction(
    plan: SystemdInstallPlan,
    *,
    pending_restart_resolution: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    if (
        pending_restart_resolution is not None
        and pending_restart_resolution not in _PENDING_RESTART_RESOLUTIONS
    ):
        emit_error(f"invalid pending-restart resolution: {pending_restart_resolution}")
        return False
    transaction_dir = _transaction_dir(plan)
    if not _path_exists(transaction_dir):
        if pending_restart_resolution is not None:
            emit_error("--resolve-pending-restart requires an existing restart_pending transaction")
            return False
        return True
    owner_live = _transaction_owner_is_live(transaction_dir)
    if owner_live is True:
        emit_error(f"another systemd install owns transaction {transaction_dir}")
        return False
    if owner_live is None:
        emit_error(f"systemd transaction owner cannot be verified; preserving {transaction_dir}")
        return False
    manifest = transaction_dir / _TRANSACTION_MANIFEST_NAME
    if not manifest.is_file():
        if pending_restart_resolution is not None:
            emit_error(
                "--resolve-pending-restart requires a pending manifest; "
                f"preserved {transaction_dir}"
            )
            return False
        # Destination mutation starts only after the durable manifest rename.
        return _remove_transaction(plan, run=run)
    print(f"recovering pending systemd transaction: {transaction_dir}")
    try:
        if pending_restart_resolution is not None and not _resolve_pending_restart(
            plan,
            pending_restart_resolution,
            run=run,
        ):
            return False
        return _rollback_pending_transaction(plan, run=run)
    except OSError as exc:
        emit_error(exc)
        return False


def _prepare_transaction(
    plan: SystemdInstallPlan,
    local_dir: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    transaction_dir = _transaction_dir(plan)
    staged_dir = transaction_dir / "staged"
    backup_dir = transaction_dir / "backup"
    local_staged = local_dir / "staged"
    local_staged.mkdir()
    for unit in plan.units:
        staged = local_staged / unit.name
        staged.write_text(unit.content, encoding="utf-8")
        staged.chmod(0o644)

    if plan.use_sudo:
        rc = _run_command(("mkdir", "-p", str(plan.unit_dir)), use_sudo=True, run=run)
        if rc != 0:
            return rc
        rc = _run_command(("mkdir", str(transaction_dir)), use_sudo=True, run=run)
        if rc != 0:
            if _path_exists(transaction_dir):
                raise _TransactionBusyError(
                    f"another systemd install owns transaction {transaction_dir}"
                )
            return rc
        rc = _write_owner_file(plan, local_dir, run=run)
        if rc != 0:
            return rc
        rc = _run_command(("mkdir", str(staged_dir), str(backup_dir)), use_sudo=True, run=run)
        if rc != 0:
            return rc
        for unit in plan.units:
            rc = _run_command(
                (
                    "install",
                    "-m",
                    "0644",
                    str(local_staged / unit.name),
                    str(staged_dir / unit.name),
                ),
                use_sudo=True,
                run=run,
            )
            if rc != 0:
                return rc
    else:
        try:
            transaction_dir.mkdir()
        except FileExistsError as exc:
            raise _TransactionBusyError(
                f"another systemd install owns transaction {transaction_dir}"
            ) from exc
        rc = _write_owner_file(plan, local_dir, run=run)
        if rc != 0:
            return rc
        staged_dir.mkdir()
        backup_dir.mkdir()
        for unit in plan.units:
            shutil.copy2(local_staged / unit.name, staged_dir / unit.name)

    backups_list: list[_UnitFileBackup] = []
    for unit in plan.units:
        original_identity = _path_identity(unit.destination)
        staged_identity = _path_identity(staged_dir / unit.name)
        if staged_identity is None:
            raise OSError(f"staged systemd unit is missing: {staged_dir / unit.name}")
        backups_list.append(
            _UnitFileBackup(
                name=unit.name,
                destination=unit.destination,
                backup=backup_dir / unit.name,
                existed=original_identity is not None,
                original_identity=original_identity,
                staged_identity=staged_identity,
            )
        )
    backups = tuple(backups_list)
    boot = _snapshot_boot_selections(plan, run=run)
    active_components = _snapshot_active_components(plan, run=run)
    rc = _write_manifest(
        plan,
        backups,
        boot,
        active_components,
        local_dir,
        run=run,
    )
    if rc != 0:
        return rc

    for item in backups:
        if not item.existed:
            continue
        if plan.use_sudo:
            rc = _run_command(
                ("mv", "--", str(item.destination), str(item.backup)),
                use_sudo=True,
                run=run,
            )
            if rc != 0:
                return rc
        else:
            os.replace(item.destination, item.backup)
    _fsync_directories((backup_dir, plan.unit_dir))
    rc = _update_manifest_fields(
        plan,
        {"unit_phase": _UNIT_PHASE_BACKUPS_DURABLE},
        run=run,
    )
    if rc != 0:
        return rc
    for unit in plan.units:
        staged = staged_dir / unit.name
        if plan.use_sudo:
            rc = _run_command(
                ("mv", "--", str(staged), str(unit.destination)),
                use_sudo=True,
                run=run,
            )
            if rc != 0:
                return rc
        else:
            os.replace(staged, unit.destination)
        print(f"installed: {unit.destination}")
    _fsync_directories((staged_dir, plan.unit_dir))
    return _update_manifest_fields(
        plan,
        {"unit_phase": _UNIT_PHASE_PUBLISHED},
        run=run,
    )


def _commit_transaction(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> _CommitResult:
    transaction_dir = _transaction_dir(plan)
    manifest = transaction_dir / _TRANSACTION_MANIFEST_NAME
    committed = transaction_dir / "committed.json"
    # Publish the commit marker by renaming manifest.json -> committed.json.
    # Recovery keys only on manifest.json's presence (committed.json is never
    # read for completeness), so only the rename's dirent must be durable, which
    # a targeted directory fsync covers -- not a whole-filesystem sync. On the
    # ext4 deployment target the ordered journal flushes the pending unit writes
    # and the systemctl enable/disable symlinks (created by plan.commands after
    # the prepare-phase fsyncs) at this same commit fsync; on a filesystem
    # without that shared-journal ordering a torn install stays bounded and
    # recoverable by re-running this manual installer.
    if plan.use_sudo:
        rc = _run_command(("mv", "--", str(manifest), str(committed)), use_sudo=True, run=run)
        if rc != 0:
            return _CommitResult(published=False, cleaned=False)
        try:
            _fsync_directories((transaction_dir,))
        except OSError as exc:
            emit_error(f"failed to make systemd commit durable: {exc}")
            return _CommitResult(published=True, cleaned=False)
    else:
        published = False
        try:
            os.replace(manifest, committed)
            published = True
            directory_fd = os.open(transaction_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            emit_error(f"failed to commit systemd transaction: {exc}")
            return _CommitResult(published=published, cleaned=False)
    return _CommitResult(
        published=True,
        cleaned=_remove_transaction(plan, run=run),
    )


def _apply_systemd_install_plan_locked(
    plan: SystemdInstallPlan,
    *,
    pending_restart_resolution: str | None = None,
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
        if not plan.use_sudo:
            plan.unit_dir.mkdir(parents=True, exist_ok=True)
        if not _recover_pending_transaction(
            plan,
            pending_restart_resolution=pending_restart_resolution,
            run=run,
        ):
            return 1
        with tempfile.TemporaryDirectory(prefix="orca_auto-systemd-local-") as local_dir_text:
            rc = _prepare_transaction(plan, Path(local_dir_text), run=run)
        if rc != 0:
            manifest = _transaction_dir(plan) / _TRANSACTION_MANIFEST_NAME
            if manifest.is_file():
                if not _rollback_pending_transaction(plan, run=run):
                    return 1
            elif not _remove_transaction(plan, run=run):
                return 1
            return rc

        for command in plan.commands:
            restart_owner = _restart_owner_for_command(plan, command)
            if restart_owner is not None:
                phase_rc = _update_manifest_fields(
                    plan,
                    {
                        "runtime_phase": _RUNTIME_PHASE_RESTART_PENDING,
                        "started_owner": restart_owner,
                    },
                    run=run,
                )
                if phase_rc != 0:
                    if not _rollback_pending_transaction(plan, run=run):
                        return 1
                    return phase_rc
            rc = _run_command(command, use_sudo=plan.use_sudo, run=run)
            if restart_owner is not None:
                phase_rc = _update_manifest_fields(
                    plan,
                    {
                        "runtime_phase": (
                            _RUNTIME_PHASE_RESTART_SUCCEEDED
                            if rc == 0
                            else _RUNTIME_PHASE_RESTART_FAILED
                        ),
                        "started_owner": restart_owner,
                    },
                    run=run,
                )
                if phase_rc != 0:
                    emit_error(
                        "systemd restart result could not be recorded durably; "
                        f"preserved recovery data at {_transaction_dir(plan)}"
                    )
                    return 1
            if rc != 0:
                if not _rollback_pending_transaction(plan, run=run):
                    return 1
                return rc
        commit_result = _commit_transaction(plan, run=run)
        if not commit_result.published:
            manifest = _transaction_dir(plan) / _TRANSACTION_MANIFEST_NAME
            if not manifest.is_file():
                emit_error(
                    "systemd install commit was not published and its recovery manifest is missing"
                )
                return 1
            if not _rollback_pending_transaction(plan, run=run):
                return 1
            emit_error(
                "systemd install commit was not published; restored the previous installation"
            )
            return 1
        if not commit_result.cleaned:
            emit_error(
                f"systemd install is committed but transaction cleanup failed; "
                f"committed state remains at {_transaction_dir(plan)}"
            )
            return 1
    except _TransactionBusyError as exc:
        emit_error(exc)
        return 1
    except OSError as exc:
        emit_error(f"failed to write systemd units or apply transaction: {exc}")
        manifest = _transaction_dir(plan) / _TRANSACTION_MANIFEST_NAME
        if manifest.is_file():
            try:
                _rollback_pending_transaction(plan, run=run)
            except OSError as rollback_exc:
                emit_error(rollback_exc)
        else:
            _remove_transaction(plan, run=run)
        return 1

    if plan.enabled_unit:
        print(f"enabled: {plan.enabled_unit}")
    else:
        print("installed systemd units; enable/start skipped")
    return 0


def apply_systemd_install_plan(
    plan: SystemdInstallPlan,
    *,
    pending_restart_resolution: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    python_path = plan.repo / ".venv" / "bin" / "python"
    if not python_path.is_file() or not os.access(python_path, os.X_OK):
        emit_error(f"service Python is missing or not executable: {python_path}; run `make venv`")
        return 1
    if plan.use_sudo and shutil.which("sudo") is None:
        emit_error("sudo is required to write system units; rerun as root or use --no-sudo")
        return 1

    lock_path = plan.repo / ".orca_auto-systemd-install.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit_error(f"another systemd install is already running for {plan.repo}")
            os.close(lock_fd)
            return 1
    except OSError as exc:
        emit_error(f"could not acquire systemd install lock {lock_path}: {exc}")
        return 1
    try:
        return _apply_systemd_install_plan_locked(
            plan,
            pending_restart_resolution=pending_restart_resolution,
            run=run,
        )
    finally:
        os.close(lock_fd)


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
    pending_restart_resolution = getattr(args, "resolve_pending_restart", None)
    if bool(getattr(args, "dry_run", False)):
        if pending_restart_resolution is not None:
            emit_error("--resolve-pending-restart cannot be used with --dry-run")
            return 1
        _print_plan(plan)
        return 0
    return int(
        apply_systemd_install_plan(
            plan,
            pending_restart_resolution=pending_restart_resolution,
            run=run,
        )
    )


__all__ = [
    "SystemdInstallCliDeps",
    "apply_systemd_install_plan",
    "cmd_systemd_install",
]
