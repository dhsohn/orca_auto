"""Process and unit evidence primitives shared by the systemd CLI commands.

Everything here reads one fact about a running unit or its main process
(``systemctl show``, ``/proc/<pid>``) and reports it without judging it. The
freshness modules and the restart guard build their verdicts on top.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto import cli_systemd_units
from orca_auto._process_evidence import PROCESS_IMPORT_SOURCE_ENV
from orca_auto.core.runtime_bundle import PROCESS_RUNTIME_BUILD_ENV


def read_process_file(path: str) -> bytes:
    return Path(path).read_bytes()


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
        completed = cli_systemd_units.show_unit_property(
            unit,
            "ExecMainStartTimestamp",
            run=run,
            extra_args=("--timestamp=utc",),
        )
    except OSError as exc:
        raise ValueError(f"systemctl show failed: {exc}") from exc
    value = cli_systemd_units.single_line_command_output(completed)
    # "Mon 2026-08-03 09:33:12 UTC" — parse without the weekday token so the
    # verdict does not depend on the CLI locale.
    tokens = value.split()
    if len(tokens) != 4 or tokens[3] != "UTC":
        raise ValueError(f"unrecognized ExecMainStartTimestamp: {value!r}")
    started = datetime.strptime(f"{tokens[1]} {tokens[2]}", "%Y-%m-%d %H:%M:%S")
    return started.replace(tzinfo=UTC).timestamp()


def parse_process_start_ticks(raw_stat: bytes, *, pid: int) -> int:
    """Field 22 (starttime) of ``/proc/<pid>/stat``: the kernel process identity."""
    # Linux proc(5) makes comm parenthesized and permits spaces (and closing
    # parentheses) inside it. Split after the final ')' so field 22 remains the
    # twentieth token in the remainder (which starts at field 3).
    closing_paren = raw_stat.rfind(b")")
    fields = raw_stat[closing_paren + 1 :].split() if closing_paren >= 0 else []
    if len(fields) <= 19:
        raise ValueError(f"invalid /proc/{pid}/stat process identity")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise ValueError(f"invalid /proc/{pid}/stat process identity") from exc
    if start_ticks <= 0:
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
    try:
        raw_environ = read_process_file(f"/proc/{pid}/environ")
    except OSError as exc:
        raise ValueError(f"cannot read /proc/{pid}/environ: {exc}") from exc
    prefix = f"{PROCESS_IMPORT_SOURCE_ENV}=".encode()
    values = [
        entry[len(prefix) :] for entry in raw_environ.split(b"\0") if entry.startswith(prefix)
    ]
    if len(values) != 1 or not values[0]:
        raise ValueError("worker import-source evidence is missing or ambiguous")
    import_source = Path(os.fsdecode(values[0])).expanduser()
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
    runtime_prefix = f"{PROCESS_RUNTIME_BUILD_ENV}=".encode()
    builds = [
        entry[len(runtime_prefix) :]
        for entry in raw_environ.split(b"\0")
        if entry.startswith(runtime_prefix)
    ]
    if len(builds) > 1:
        raise ValueError("worker runtime build evidence is ambiguous")
    return WorkerImportEvidence(
        import_source=import_source,
        process_start_ticks=start_ticks_before,
        runtime_build_id=os.fsdecode(builds[0]) if builds else "",
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
    """One active worker's staleness judgement: which payload list it joins."""

    kind: str  # "worker" | "undetermined" | "uncompared"
    row: dict[str, Any]
    stale: bool = False


__all__ = [
    "WorkerImportEvidence",
    "WorkerVerdict",
    "parse_process_start_ticks",
    "process_identity_race_detail",
    "read_process_file",
    "read_process_start_ticks",
    "unit_main_pid",
    "unit_start_epoch",
    "worker_process_import_evidence",
]
