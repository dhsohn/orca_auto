"""Idle-only restart guard for the installed, shared-admission worker services."""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto import cli_systemd_freshness, cli_systemd_units
from orca_auto.core.admission import (
    AdmissionStoreCorruptError,
    admission_lock,
    read_active_slot_count,
)
from orca_auto.core.admission.persistence import ADMISSION_LOCK_NAME
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config.bounded_yaml import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.orca.config import load_config


@dataclass(frozen=True)
class _WorkerBinding:
    unit: str
    config: Path
    config_identity: tuple[int, int, int, int, int]
    admission_root: Path
    pid: int
    start_ticks: int | None
    started_epoch: float | None


def _property(
    unit: str,
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    try:
        completed = cli_systemd_units._show_unit_property(unit, name, run=run)
    except OSError:
        raise ValueError(f"Cannot inspect {name} for {unit}.") from None
    if completed.returncode != 0 or str(completed.stderr or "").strip():
        raise ValueError(f"Cannot inspect {name} for {unit}.")
    return str(completed.stdout or "").strip()


def _installed_config(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> Path:
    if _property(unit, "EnvironmentFiles", run=run) or _property(unit, "UnsetEnvironment", run=run):
        raise ValueError(f"Cannot verify overridden service configuration for {unit}.")
    try:
        environment = shlex.split(_property(unit, "Environment", run=run))
    except ValueError:
        raise ValueError(f"Cannot read service configuration binding for {unit}.") from None
    prefix = f"{ORCA_AUTO_CONFIG_ENV_VAR}="
    values = [item[len(prefix) :] for item in environment if item.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"Missing or ambiguous service configuration binding for {unit}.")
    config = Path(values[0])
    try:
        if not config.is_absolute() or config.resolve(strict=True) != config:
            raise ValueError
        if not stat.S_ISREG(config.lstat().st_mode):
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise ValueError(
            f"Service configuration must be an existing absolute regular file for {unit}."
        ) from None

    command = _property(unit, "ExecStart", run=run)
    argv_matches = re.findall(r"\bargv\[\]=(.*?)\s*;", command)
    executable_matches = re.findall(r"\bpath=(.*?)\s*;", command)
    if len(argv_matches) != 1 or len(executable_matches) != 1:
        raise ValueError(f"Cannot verify installed worker command for {unit}.")
    try:
        argv = shlex.split(argv_matches[0])
    except ValueError:
        raise ValueError(f"Cannot verify installed worker command for {unit}.") from None
    app = "workflow" if unit.startswith("orca_auto-workflow-worker@") else "orca"
    if (
        len(argv) != 7
        or not Path(argv[0]).is_absolute()
        or not argv[0].endswith("/.venv/bin/python")
        or executable_matches[0] != argv[0]
        or argv[1:] != ["-m", "orca_auto.cli", "queue", "worker", "--app", app]
    ):
        raise ValueError(f"Unsupported worker command or custom configuration override for {unit}.")
    return config


def _config_identity(config: Path) -> tuple[int, int, int, int, int]:
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or config.resolve(strict=True) != config:
        raise ValueError("Service configuration identity changed.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _worker_binding(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> _WorkerBinding:
    config = _installed_config(unit, run=run)
    try:
        identity = _config_identity(config)
        cfg = load_config(str(config))
        root = Path(cfg.runtime.resolved_admission_root).resolve(strict=True)
        if _config_identity(config) != identity:
            raise ValueError
    except (*YAML_CONFIG_LOAD_EXCEPTIONS, RuntimeError):
        raise ValueError(f"Cannot verify current configuration for {unit}.") from None
    pid_text = _property(unit, "MainPID", run=run)
    if not pid_text.isdecimal():
        raise ValueError(f"Cannot verify worker process identity for {unit}.")
    pid = int(pid_text)
    ticks: int | None = None
    started: float | None = None
    if pid:
        try:
            ticks = cli_systemd_freshness._read_process_start_ticks(
                pid, read_process_file=read_process_file
            )
            environment = read_process_file(f"/proc/{pid}/environ")
            prefix = f"{ORCA_AUTO_CONFIG_ENV_VAR}=".encode()
            values = [
                item[len(prefix) :] for item in environment.split(b"\0") if item.startswith(prefix)
            ]
            if values != [os.fsencode(str(config))]:
                raise ValueError
            if (
                cli_systemd_freshness._read_process_start_ticks(
                    pid, read_process_file=read_process_file
                )
                != ticks
            ):
                raise ValueError

            def checked_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
                result = run(*args, **kwargs)
                if result.returncode != 0 or str(result.stderr or "").strip():
                    raise ValueError
                return result

            started = cli_systemd_freshness._unit_start_epoch(unit, run=checked_run)
            # This conservative timestamp check rejects ordinary live config
            # edits; it is not a frozen copy of the process's loaded config.
            # Operating policy still forbids reconfiguring running workers.
            if max(identity[3:]) > int(started * 1_000_000_000):
                raise ValueError
            if _property(unit, "MainPID", run=run) != pid_text:
                raise ValueError
        except (OSError, ValueError):
            raise ValueError(f"Cannot verify unchanged running configuration for {unit}.") from None
    return _WorkerBinding(unit, config, identity, root, pid, ticks, started)


def _root_identity(root: Path) -> tuple[int, int, int, int]:
    directory = root.lstat()
    lock = (root / ADMISSION_LOCK_NAME).lstat()
    if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1:
        raise ValueError("Admission root and its existing regular lock are required.")
    return directory.st_dev, directory.st_ino, lock.st_dev, lock.st_ino


@contextmanager
def guard_service_restart(
    worker_units: tuple[str, ...],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    read_process_file: Callable[[str], bytes] = cli_systemd_freshness._read_process_file,
) -> Iterator[None]:
    """Hold existing admission locks from verified idleness through restart.

    This is an idle-only guard, not a drain: live reservations and unresolved
    process ownership refuse immediately. No queue or admission records are
    reconciled, and no missing admission directory or lock is initialized.
    """
    if not worker_units:
        raise ValueError("No worker services were selected for restart.")
    bindings = tuple(
        _worker_binding(unit, run=run, read_process_file=read_process_file) for unit in worker_units
    )
    roots = sorted({binding.admission_root for binding in bindings})
    with ExitStack() as stack:
        try:
            identities = {root: _root_identity(root) for root in roots}
            for root in roots:
                stack.enter_context(admission_lock(root))
                if _root_identity(root) != identities[root]:
                    raise ValueError("Admission root or lock changed during restart inspection.")
        except (OSError, RuntimeError, ValueError):
            raise ValueError(
                "Cannot lock existing service admission state for a safe restart."
            ) from None
        for before in bindings:
            after = _worker_binding(before.unit, run=run, read_process_file=read_process_file)
            if after != before:
                raise ValueError(f"Service configuration or process changed for {before.unit}.")
        try:
            active_count = sum(read_active_slot_count(root) for root in roots)
        except (AdmissionStoreCorruptError, OSError, ValueError):
            raise ValueError("Cannot read service admission state for a safe restart.") from None
        if active_count:
            raise ValueError(f"{active_count} active or reserved simulations remain.")
        yield


__all__ = ["guard_service_restart"]
