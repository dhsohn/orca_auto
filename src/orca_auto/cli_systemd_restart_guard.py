"""Idle-only restart guard for the installed worker service and its one admission store."""

from __future__ import annotations

import re
import shlex
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto import cli_systemd_evidence
from orca_auto.core.admission import (
    AdmissionStoreCorruptError,
    admission_dir,
    admission_lock,
    read_active_slot_count,
)
from orca_auto.core.admission.persistence import ADMISSION_LOCK_NAME
from orca_auto.core.config.files import ORCA_AUTO_CONFIG_ENV_VAR, YAML_CONFIG_LOAD_EXCEPTIONS
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


def _installed_config(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> Path:
    cli_systemd_evidence.refuse_environment_overrides(unit, run=run)
    values = cli_systemd_evidence.unit_environment_values(unit, ORCA_AUTO_CONFIG_ENV_VAR, run=run)
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

    command = cli_systemd_evidence.strict_unit_property(unit, "ExecStart", run=run)
    argv_matches = re.findall(r"\bargv\[\]=(.*?)\s*;", command)
    executable_matches = re.findall(r"\bpath=(.*?)\s*;", command)
    if len(argv_matches) != 1 or len(executable_matches) != 1:
        raise ValueError(f"Cannot verify installed worker command for {unit}.")
    try:
        argv = shlex.split(argv_matches[0])
    except ValueError:
        raise ValueError(f"Cannot verify installed worker command for {unit}.") from None
    worker_args = ["-m", "orca_auto.cli", "queue", "worker"]
    if (
        len(argv) not in {5, 6}
        or not Path(argv[0]).is_absolute()
        or not argv[0].endswith("/.venv/bin/python")
        or executable_matches[0] != argv[0]
        or argv[1:] not in (worker_args, ["-I", *worker_args])
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
        root = admission_dir(cfg.runtime.allowed_root).resolve(strict=True)
        if _config_identity(config) != identity:
            raise ValueError
    except (*YAML_CONFIG_LOAD_EXCEPTIONS, RuntimeError):
        raise ValueError(f"Cannot verify current configuration for {unit}.") from None
    pid_text = cli_systemd_evidence.strict_unit_property(unit, "MainPID", run=run)
    if not pid_text.isdecimal():
        raise ValueError(f"Cannot verify worker process identity for {unit}.")
    pid = int(pid_text)
    ticks: int | None = None
    started: float | None = None
    if pid:
        try:
            ticks = cli_systemd_evidence.read_process_start_ticks(
                pid, read_process_file=read_process_file
            )
            values = cli_systemd_evidence.process_environ_values(
                pid, ORCA_AUTO_CONFIG_ENV_VAR, read_process_file=read_process_file
            )
            if values != [str(config)]:
                raise ValueError
            if (
                cli_systemd_evidence.read_process_start_ticks(
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

            started = cli_systemd_evidence.unit_start_epoch(unit, run=checked_run)
            # This conservative timestamp check rejects ordinary live config
            # edits; it is not a frozen copy of the process's loaded config.
            # Operating policy still forbids reconfiguring running workers.
            if max(identity[3:]) > int(started * 1_000_000_000):
                raise ValueError
            if cli_systemd_evidence.strict_unit_property(unit, "MainPID", run=run) != pid_text:
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
    worker_unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    read_process_file: Callable[[str], bytes] = cli_systemd_evidence.read_process_file,
) -> Iterator[None]:
    """Hold the existing admission lock from verified idleness through restart.

    This is an idle-only guard, not a drain: live reservations and unresolved
    process ownership refuse immediately. No queue or admission records are
    reconciled, and no missing admission directory or lock is initialized.
    """
    binding = _worker_binding(worker_unit, run=run, read_process_file=read_process_file)
    root = binding.admission_root
    with ExitStack() as stack:
        try:
            identity = _root_identity(root)
            stack.enter_context(admission_lock(root))
            if _root_identity(root) != identity:
                raise ValueError("Admission root or lock changed during restart inspection.")
        except (OSError, RuntimeError, ValueError):
            raise ValueError(
                "Cannot lock existing service admission state for a safe restart."
            ) from None
        after = _worker_binding(worker_unit, run=run, read_process_file=read_process_file)
        if after != binding:
            raise ValueError(f"Service configuration or process changed for {worker_unit}.")
        try:
            active_count = read_active_slot_count(root)
        except (AdmissionStoreCorruptError, OSError, ValueError):
            raise ValueError("Cannot read service admission state for a safe restart.") from None
        if active_count:
            raise ValueError(f"{active_count} active or reserved simulations remain.")
        yield


__all__ = ["guard_service_restart"]
