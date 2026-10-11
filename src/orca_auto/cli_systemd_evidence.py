"""Process and unit evidence primitives shared by the systemd CLI commands.

Everything here reads one fact about a running unit or its main process
(``systemctl show``, ``/proc/<pid>``) and reports it without judging it. The
restart guard and the freshness modules read unit evidence through these
readers and build their verdicts on top; ``WorkerVerdict`` is the record those
freshness judges return, kept here because both deployment models produce it.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto import cli_systemd_units
from orca_auto._process_evidence import PROCESS_IMPORT_SOURCE_ENV
from orca_auto.core.runtime_bundle import PROCESS_RUNTIME_BUILD_ENV
from orca_auto.core.utils import normalize_text
from orca_auto.core.utils import process as process_utils


def read_process_file(path: str) -> bytes:
    return Path(path).read_bytes()


def strict_unit_property(
    unit: str,
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """One ``systemctl show`` property; ValueError unless systemd answered cleanly."""
    try:
        completed = cli_systemd_units.show_unit_property(unit, name, run=run)
    except OSError:
        raise ValueError(f"Cannot inspect {name} for {unit}.") from None
    if completed.returncode != 0 or normalize_text(completed.stderr):
        raise ValueError(f"Cannot inspect {name} for {unit}.")
    return str(completed.stdout or "").strip()


def refuse_environment_overrides(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    """ValueError when drop-in environment files or unset names can change the unit's environment."""
    if strict_unit_property(unit, "EnvironmentFiles", run=run) or strict_unit_property(
        unit, "UnsetEnvironment", run=run
    ):
        raise ValueError(f"Cannot verify overridden service configuration for {unit}.")


def environment_values(environment: str, key: str, *, unit: str) -> list[str]:
    """Every value ``key`` takes in ``environment``, one ``Environment`` text of ``unit``."""
    try:
        items = shlex.split(environment)
    except ValueError:
        raise ValueError(f"Cannot parse Environment for {unit}.") from None
    prefix = f"{key}="
    return [item[len(prefix) :] for item in items if item.startswith(prefix)]


def unit_environment_values(
    unit: str,
    key: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    """Every value ``key`` takes in the unit's ``Environment=``."""
    return environment_values(strict_unit_property(unit, "Environment", run=run), key, unit=unit)


def process_environ(
    pid: int,
    *,
    read_process_file: Callable[[str], bytes] = read_process_file,
) -> dict[str, list[str]]:
    """Every value each name takes in ``/proc/<pid>/environ``, read once."""
    try:
        raw_environ = read_process_file(f"/proc/{pid}/environ")
    except OSError as exc:
        raise ValueError(f"cannot read /proc/{pid}/environ: {exc}") from exc
    environ: dict[str, list[str]] = {}
    for entry in raw_environ.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if separator:
            environ.setdefault(os.fsdecode(name), []).append(os.fsdecode(value))
    return environ


def unit_main_pid(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """The unit's MainPID, or 0 when systemd cannot report one."""
    try:
        completed = cli_systemd_units.show_unit_property(unit, "MainPID", run=run)
    except OSError:
        return 0
    try:
        return int(cli_systemd_units.single_line_command_output(completed))
    except ValueError:
        return 0


def unit_start_epoch(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> float:
    """Main-process start in epoch seconds, for display and coarse comparisons."""
    return unit_start_epoch_ns(unit, run=run) / 1_000_000_000


def unit_start_epoch_ns(
    unit: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Exact main-process start in epoch nanoseconds, from systemd's record.

    systemd snapshots CLOCK_REALTIME when it forks the main process, so the
    value stays true after later clock steps. Deriving the start from
    ``/proc/<pid>/stat`` ticks plus ``btime`` does not: ``btime`` is recomputed
    from the current wall clock minus the monotonic uptime, and on WSL2 the
    wall clock is stepped forward after host sleeps while the monotonic clock
    stood still, which shifts every derived start time forward and can mask a
    genuinely stale worker (observed live: +32 min).
    """
    try:
        completed = cli_systemd_units.show_unit_property(
            unit,
            "ExecMainStartTimestamp",
            run=run,
            extra_args=("--timestamp=us+utc",),
        )
    except OSError as exc:
        raise ValueError(f"systemctl show failed: {exc}") from exc
    if completed.returncode != 0 or normalize_text(completed.stderr):
        raise ValueError("systemctl show failed to report a clean start timestamp")
    value = str(completed.stdout or "")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    # Ignore the weekday token to avoid depending on the CLI locale. Accept
    # whole seconds conservatively, but never truncate fractional evidence or
    # accept extra lines. us+utc normally prints exactly six fractional digits.
    match = re.fullmatch(
        r"\S+ ([0-9]{4}-[0-9]{2}-[0-9]{2} "
        r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?) UTC",
        value,
    )
    if match is None:
        raise ValueError(f"unrecognized ExecMainStartTimestamp: {value!r}")
    stamp = match.group(1)
    format_string = "%Y-%m-%d %H:%M:%S.%f" if "." in stamp else "%Y-%m-%d %H:%M:%S"
    started = datetime.strptime(stamp, format_string).replace(tzinfo=UTC)
    elapsed = started - datetime(1970, 1, 1, tzinfo=UTC)
    # datetime.timestamp() is a float: a float round-trip can move the boundary
    # by hundreds of nanoseconds and hide a config edit just after startup.
    return ((elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds) * 1_000


def parse_process_start_ticks(raw_stat: bytes, *, pid: int) -> int:
    """Field 22 (starttime) of ``/proc/<pid>/stat``: the kernel process identity."""
    start_ticks = process_utils.parse_stat_start_ticks(raw_stat.decode("utf-8", errors="ignore"))
    if start_ticks is None:
        raise ValueError(f"invalid /proc/{pid}/stat process identity")
    return start_ticks


def read_process_start_ticks(
    pid: int,
    *,
    read_process_file: Callable[[str], bytes] = read_process_file,
) -> int:
    try:
        raw_stat = read_process_file(f"/proc/{pid}/stat")
    except OSError as exc:
        raise ValueError(f"cannot read /proc/{pid}/stat: {exc}") from exc
    return parse_process_start_ticks(raw_stat, pid=pid)


@dataclass(frozen=True)
class WorkerImportEvidence:
    """What a worker process published about itself at exec time."""

    import_source: Path
    process_start_ticks: int
    runtime_build_id: str = ""


def worker_process_import_evidence(
    pid: int,
    *,
    read_process_file: Callable[[str], bytes] = read_process_file,
) -> WorkerImportEvidence:
    """Read the re-exec evidence from ``/proc/<pid>/environ``.

    The process start ticks are read before and after the environ so a PID
    reused by another process during the read is rejected rather than trusted.
    """
    start_ticks_before = read_process_start_ticks(pid, read_process_file=read_process_file)
    environ = process_environ(pid, read_process_file=read_process_file)
    values = environ.get(PROCESS_IMPORT_SOURCE_ENV, [])
    builds = environ.get(PROCESS_RUNTIME_BUILD_ENV, [])
    if len(values) != 1 or not values[0]:
        raise ValueError("worker import-source evidence is missing or ambiguous")
    import_source = Path(values[0]).expanduser()
    if not import_source.is_absolute():
        raise ValueError(f"worker import source is not absolute: {import_source!s}")
    try:
        import_source = import_source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve worker import source {import_source}: {exc}") from exc
    if not import_source.is_file():
        raise ValueError(f"worker import source is not a file: {import_source}")
    start_ticks_after = read_process_start_ticks(pid, read_process_file=read_process_file)
    if start_ticks_after != start_ticks_before:
        raise ValueError("worker process identity changed during freshness inspection")
    if len(builds) > 1:
        raise ValueError("worker runtime build evidence is ambiguous")
    return WorkerImportEvidence(
        import_source=import_source,
        process_start_ticks=start_ticks_before,
        runtime_build_id=builds[0] if builds else "",
    )


def process_identity_race_detail(
    unit: str,
    *,
    pid: int,
    process_start_ticks: int,
    run: Callable[..., subprocess.CompletedProcess[Any]],
    read_process_file: Callable[[str], bytes],
) -> str:
    """Why the observed process is no longer the one being judged, or ``""``."""
    if unit_main_pid(unit, run=run) != pid:
        return "main PID changed during freshness inspection"
    try:
        observed_ticks = read_process_start_ticks(pid, read_process_file=read_process_file)
    except ValueError as exc:
        return f"cannot re-read worker process identity: {exc}"
    if observed_ticks != process_start_ticks:
        return "worker process identity changed during freshness inspection"
    return ""


@dataclass(frozen=True)
class WorkerVerdict:
    """One active worker's staleness judgement: which payload list it joins.

    ``stale_explanation`` says why a stale worker fails ``service status``; it
    is empty exactly when the worker is not stale.
    """

    kind: str  # "worker" | "undetermined" | "uncompared"
    row: dict[str, Any]
    stale_explanation: str = ""


__all__ = [
    "WorkerImportEvidence",
    "WorkerVerdict",
    "environment_values",
    "parse_process_start_ticks",
    "process_environ",
    "process_identity_race_detail",
    "read_process_file",
    "read_process_start_ticks",
    "refuse_environment_overrides",
    "strict_unit_property",
    "unit_environment_values",
    "unit_main_pid",
    "unit_start_epoch",
    "unit_start_epoch_ns",
    "worker_process_import_evidence",
]
