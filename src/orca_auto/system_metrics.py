"""Dependency-free ``/proc`` system metrics for the ``queue list --watch`` view.

The project keeps a minimal runtime footprint, so rather than depend on
``psutil`` this module reads the Linux ``/proc`` pseudo-files directly. It powers
the live CPU / RAM / load gauges the watch loop draws above the queue table.

Everything fails closed: any missing or malformed file yields ``None`` so callers
simply omit the resource line on a non-Linux host (no ``/proc``) or when a file
cannot be read — never a crash and never a fabricated number. CPU utilization is
a delta measurement, so :class:`SystemMetricsSampler` returns ``cpu_percent=None``
until it has two samples (the watch loop supplies the second on its next refresh).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

_PROC = Path("/proc")
_SampleIdentityT = TypeVar("_SampleIdentityT", bound=Hashable)
_MAX_PROC_INTEGER = (1 << 64) - 1

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX fallback
    _CLK_TCK = 100
try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX fallback
    _PAGE_SIZE = 4096


@dataclass(frozen=True)
class CpuTimes:
    """Aggregate jiffies from the ``cpu`` line of ``/proc/stat``."""

    total: int
    idle: int


@dataclass(frozen=True)
class SystemMetrics:
    cpu_percent: float | None
    mem_used_bytes: int | None
    mem_total_bytes: int | None
    load1: float | None
    load5: float | None
    load15: float | None


def parse_cpu_times(stat_text: str) -> CpuTimes | None:
    """Parse the aggregate ``cpu`` line of ``/proc/stat`` into totals.

    ``guest``/``guest_nice`` (fields 9-10) are already counted inside
    ``user``/``nice``, so only the first eight fields are summed to avoid
    double-counting. ``idle`` is ``idle + iowait``.
    """

    for line in stat_text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()[1:]
        if len(fields) < 5:
            return None
        try:
            values = [int(field) for field in fields[:8]]
        except ValueError:
            return None
        if any(value < 0 or value > _MAX_PROC_INTEGER for value in values):
            return None
        total = sum(values)
        idle = values[3] + values[4]
        if total <= 0:
            return None
        return CpuTimes(total=total, idle=idle)
    return None


def cpu_percent_between(previous: CpuTimes, current: CpuTimes) -> float | None:
    """Busy-time percentage between two :class:`CpuTimes` snapshots (0-100)."""

    total_delta = current.total - previous.total
    idle_delta = current.idle - previous.idle
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None
    percent = (total_delta - idle_delta) / total_delta * 100.0
    return max(0.0, min(100.0, percent))


def _meminfo_kb(line: str) -> int | None:
    parts = line.split()
    if len(parts) < 3 or parts[2] != "kB":
        return None
    try:
        value = int(parts[1])
    except ValueError:
        return None
    return value if 0 <= value <= _MAX_PROC_INTEGER else None


def parse_meminfo(meminfo_text: str) -> tuple[int, int] | None:
    """Return ``(used_bytes, total_bytes)`` from ``/proc/meminfo``.

    ``used`` is ``MemTotal - MemAvailable`` — the kernel's own estimate of memory
    that cannot be reclaimed, which tracks real pressure better than
    ``MemFree``.
    """

    total_kb: int | None = None
    available_kb: int | None = None
    for line in meminfo_text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = _meminfo_kb(line)
        elif line.startswith("MemAvailable:"):
            available_kb = _meminfo_kb(line)
        if total_kb is not None and available_kb is not None:
            break
    if total_kb is None or available_kb is None or total_kb <= 0 or available_kb > total_kb:
        return None
    used_kb = max(0, total_kb - available_kb)
    return used_kb * 1024, total_kb * 1024


def parse_loadavg(loadavg_text: str) -> tuple[float, float, float] | None:
    parts = loadavg_text.split()
    if len(parts) < 3:
        return None
    try:
        values = (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None
    # ``float()`` also accepts ``inf``/``nan``; reject non-finite so a corrupt
    # file degrades to "no load" rather than rendering a bogus literal.
    if not all(math.isfinite(value) and value >= 0 for value in values):
        return None
    return values


def _read_proc(name: str) -> str | None:
    try:
        return (_PROC / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_cpu_times() -> CpuTimes | None:
    text = _read_proc("stat")
    return parse_cpu_times(text) if text is not None else None


def read_meminfo() -> tuple[int, int] | None:
    text = _read_proc("meminfo")
    return parse_meminfo(text) if text is not None else None


def read_loadavg() -> tuple[float, float, float] | None:
    text = _read_proc("loadavg")
    return parse_loadavg(text) if text is not None else None


class SystemMetricsSampler:
    """Stateful sampler that turns successive ``/proc`` reads into metrics.

    The reader callables are injectable so the sampling logic can be tested
    without a real ``/proc``. :meth:`sample` returns ``None`` only when every
    source is unavailable (e.g. no ``/proc`` at all); otherwise it returns a
    :class:`SystemMetrics` with ``None`` for whichever individual source failed.
    """

    def __init__(
        self,
        *,
        read_cpu: Callable[[], CpuTimes | None] = read_cpu_times,
        read_mem: Callable[[], tuple[int, int] | None] = read_meminfo,
        read_load: Callable[[], tuple[float, float, float] | None] = read_loadavg,
    ) -> None:
        self._read_cpu = read_cpu
        self._read_mem = read_mem
        self._read_load = read_load
        self._previous_cpu: CpuTimes | None = None

    def sample(self) -> SystemMetrics | None:
        cpu = self._read_cpu()
        cpu_percent: float | None = None
        if cpu is not None:
            if self._previous_cpu is not None:
                cpu_percent = cpu_percent_between(self._previous_cpu, cpu)
            self._previous_cpu = cpu

        mem = self._read_mem()
        load = self._read_load()
        if cpu is None and mem is None and load is None:
            return None

        used_bytes, total_bytes = mem if mem is not None else (None, None)
        load1, load5, load15 = load if load is not None else (None, None, None)
        return SystemMetrics(
            cpu_percent=cpu_percent,
            mem_used_bytes=used_bytes,
            mem_total_bytes=total_bytes,
            load1=load1,
            load5=load5,
            load15=load15,
        )


@dataclass(frozen=True)
class JobMetrics:
    """Per-process-group resource usage for one running job."""

    cpu_percent: float | None
    rss_bytes: int


def parse_pid_stat(stat_text: str) -> tuple[int, int, int] | None:
    """Parse ``/proc/<pid>/stat`` into ``(pgrp, cpu_jiffies, rss_pages)``.

    ``comm`` (field 2) may contain spaces or parentheses, so the fields after it
    are taken from the text following the *last* ``)``. Field offsets (1-indexed):
    5=pgrp, 14=utime, 15=stime, 24=rss (pages). Returns ``None`` on any malformed
    input.
    """

    end = stat_text.rfind(")")
    if end == -1:
        return None
    after = stat_text[end + 1 :].split()
    # after[0] is field 3 (state); pgrp=field5→after[2], utime=field14→after[11],
    # stime=field15→after[12], rss=field24→after[21].
    if len(after) < 22:
        return None
    try:
        pgrp = int(after[2])
        utime = int(after[11])
        stime = int(after[12])
        rss_pages = int(after[21])
    except ValueError:
        return None
    if (
        pgrp <= 0
        or any(value < 0 or value > _MAX_PROC_INTEGER for value in (utime, stime, rss_pages))
        or utime + stime > _MAX_PROC_INTEGER
    ):
        return None
    cpu_jiffies = utime + stime
    return pgrp, cpu_jiffies, rss_pages


def read_process_group_usage(
    pgids: Iterable[int],
    *,
    proc_root: Path | None = None,
    page_size: int | None = None,
) -> dict[int, tuple[int, int]]:
    """Aggregate ``(cpu_jiffies, rss_bytes)`` per target process group.

    Scans ``/proc`` once and buckets every live process by its process group,
    keeping only the requested ``pgids``. Processes that exit mid-scan are
    skipped. Returns only the process groups that had at least one live member,
    so an exited job simply drops out (fail closed).
    """

    targets = {int(pgid) for pgid in pgids if pgid and int(pgid) > 0}
    if not targets:
        return {}
    root = proc_root if proc_root is not None else _PROC
    page = page_size if page_size is not None else _PAGE_SIZE
    if type(page) is not int or page <= 0:
        return {}
    totals: dict[int, list[int]] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            content = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # process exited between listing and read
        parsed = parse_pid_stat(content)
        if parsed is None:
            continue
        pgrp, cpu_jiffies, rss_pages = parsed
        if pgrp not in targets:
            continue
        bucket = totals.setdefault(pgrp, [0, 0])
        bucket[0] += cpu_jiffies
        bucket[1] += rss_pages * page
    return {pgid: (cpu, rss) for pgid, (cpu, rss) in totals.items()}


class ProcessGroupSampler:
    """Stateful per-process-group CPU/RSS sampler.

    ``cpu_percent`` is a delta measurement (percent of one core, ``top`` style, so
    a multithreaded job can exceed 100%), needing two samples; ``rss_bytes`` is
    instantaneous. The wall clock (``now``) is injected so callers control the
    interval and tests are deterministic. Callers key each group by a stable,
    boot-scoped identity. Groups or identities that disappear are forgotten so
    state cannot grow unbounded or reuse a stale delta when a numeric PGID is
    recycled.
    """

    def __init__(
        self,
        *,
        read_usage: Callable[
            [Iterable[int]], dict[int, tuple[int, int]]
        ] = read_process_group_usage,
        clk_tck: int | None = None,
    ) -> None:
        self._read_usage = read_usage
        self._clk_tck = clk_tck if clk_tck is not None else _CLK_TCK
        self._previous: dict[Hashable, tuple[int, int, float]] = {}

    def sample(
        self,
        pgids_by_identity: Mapping[_SampleIdentityT, int],
        *,
        now: float,
    ) -> dict[_SampleIdentityT, JobMetrics]:
        normalized = {
            identity: pgid
            for identity, pgid in pgids_by_identity.items()
            if type(pgid) is int and pgid > 0
        }
        current = self._read_usage(set(normalized.values()))
        result: dict[_SampleIdentityT, JobMetrics] = {}
        next_previous: dict[Hashable, tuple[int, int, float]] = {}
        for identity, pgid in normalized.items():
            usage = current.get(pgid)
            if usage is None:
                continue
            cpu_jiffies, rss_bytes = usage
            cpu_percent: float | None = None
            previous = self._previous.get(identity)
            if previous is not None and previous[0] == pgid and self._clk_tck > 0:
                _previous_pgid, previous_jiffies, previous_now = previous
                elapsed = now - previous_now
                delta = cpu_jiffies - previous_jiffies
                if elapsed > 0 and delta >= 0:
                    cpu_percent = max(0.0, (delta / self._clk_tck) / elapsed * 100.0)
            next_previous[identity] = (pgid, cpu_jiffies, now)
            result[identity] = JobMetrics(cpu_percent=cpu_percent, rss_bytes=rss_bytes)
        self._previous = next_previous
        return result


__all__ = [
    "CpuTimes",
    "JobMetrics",
    "ProcessGroupSampler",
    "SystemMetrics",
    "SystemMetricsSampler",
    "cpu_percent_between",
    "parse_cpu_times",
    "parse_loadavg",
    "parse_meminfo",
    "parse_pid_stat",
    "read_cpu_times",
    "read_loadavg",
    "read_meminfo",
    "read_process_group_usage",
]
