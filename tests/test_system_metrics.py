from __future__ import annotations

import os

import pytest

from orca_auto import system_metrics as sm
from orca_auto.system_metrics import CpuTimes, SystemMetricsSampler


def test_parse_cpu_times_sums_first_eight_fields_and_idle_iowait() -> None:
    times = sm.parse_cpu_times("cpu  10 20 30 40 50 60 70 80 90 100\ncpu0 1 2 3 4\n")
    assert times == CpuTimes(total=10 + 20 + 30 + 40 + 50 + 60 + 70 + 80, idle=40 + 50)


def test_parse_cpu_times_fails_closed_on_bad_input() -> None:
    assert sm.parse_cpu_times("cpu  10 20 x 40 50") is None  # non-integer
    assert sm.parse_cpu_times("cpu  1 2 3") is None  # too few fields
    assert sm.parse_cpu_times("intr 1 2 3 4 5") is None  # no cpu line
    assert sm.parse_cpu_times("cpu  0 0 0 0 0") is None  # zero total
    assert sm.parse_cpu_times("cpu  10 -1 3 4 5") is None


def test_cpu_percent_between_is_clamped_and_guards_nonpositive_delta() -> None:
    assert sm.cpu_percent_between(CpuTimes(100, 80), CpuTimes(200, 130)) == 50.0
    # No elapsed jiffies -> undefined -> None.
    assert sm.cpu_percent_between(CpuTimes(100, 80), CpuTimes(100, 80)) is None
    # Counter rollback or idle advancing beyond total is not a valid sample.
    assert sm.cpu_percent_between(CpuTimes(100, 50), CpuTimes(200, 40)) is None
    assert sm.cpu_percent_between(CpuTimes(100, 50), CpuTimes(200, 250)) is None


def test_parse_meminfo_uses_available_and_reports_bytes() -> None:
    result = sm.parse_meminfo("MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 400 kB\n")
    assert result is not None
    used, total = result
    assert total == 1000 * 1024
    assert used == (1000 - 400) * 1024


def test_parse_meminfo_fails_closed() -> None:
    assert sm.parse_meminfo("MemTotal: 1000 kB\n") is None  # no MemAvailable
    assert sm.parse_meminfo("MemTotal: 0 kB\nMemAvailable: 0 kB\n") is None  # zero total
    assert sm.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 101 kB\n") is None
    assert sm.parse_meminfo("MemTotal: 100 bytes\nMemAvailable: 50 bytes\n") is None
    assert sm.parse_meminfo("garbage") is None


def test_parse_loadavg() -> None:
    assert sm.parse_loadavg("1.02 1.35 1.64 1/592 863513") == (1.02, 1.35, 1.64)
    assert sm.parse_loadavg("1.0 2.0") is None
    assert sm.parse_loadavg("a b c") is None
    # ``float()`` parses these, but a non-finite load is corrupt -> fail closed.
    assert sm.parse_loadavg("inf nan 1.0") is None
    assert sm.parse_loadavg("-1.0 0.0 1.0") is None


def test_sampler_cpu_percent_needs_two_samples() -> None:
    cpu_reads = iter([CpuTimes(total=100, idle=80), CpuTimes(total=200, idle=130)])
    sampler = SystemMetricsSampler(
        read_cpu=lambda: next(cpu_reads),
        read_mem=lambda: (500, 1000),
        read_load=lambda: (1.0, 2.0, 3.0),
    )
    first = sampler.sample()
    assert first is not None and first.cpu_percent is None
    second = sampler.sample()
    assert second is not None and second.cpu_percent == 50.0
    assert second.mem_used_bytes == 500 and second.mem_total_bytes == 1000
    assert (second.load1, second.load5, second.load15) == (1.0, 2.0, 3.0)


def test_sampler_returns_none_when_all_sources_unavailable() -> None:
    sampler = SystemMetricsSampler(
        read_cpu=lambda: None, read_mem=lambda: None, read_load=lambda: None
    )
    assert sampler.sample() is None


def test_sampler_reports_partial_sources() -> None:
    sampler = SystemMetricsSampler(
        read_cpu=lambda: None, read_mem=lambda: (500, 1000), read_load=lambda: None
    )
    metrics = sampler.sample()
    assert metrics is not None
    assert metrics.cpu_percent is None
    assert metrics.mem_used_bytes == 500 and metrics.mem_total_bytes == 1000
    assert metrics.load1 is None


def test_parse_pid_stat_handles_comm_with_spaces_and_parens() -> None:
    # comm is "(my proc (x))" — parsing must key off the LAST ')'.
    stat = "123 (my proc (x)) R 1 100 100 0 100 0 0 0 0 0 50 30 7 3 20 0 1 0 999 123456 200"
    assert sm.parse_pid_stat(stat) == (100, 50 + 30 + 7 + 3, 200)


def test_parse_pid_stat_fails_closed() -> None:
    assert sm.parse_pid_stat("no closing paren") is None
    assert sm.parse_pid_stat("1 (x) R 1 2") is None  # too few fields after comm
    assert sm.parse_pid_stat("1 (x) R 1 10 10 0 10 0 0 0 0 0 -1 1 0 0 20 0 1 0 1 1 1") is None
    assert sm.parse_pid_stat("1 (x) R 1 10 10 0 10 0 0 0 0 0 1 1 -1 0 20 0 1 0 1 1 1") is None
    too_large = str((1 << 64) - 1)
    assert (
        sm.parse_pid_stat(f"1 (x) R 1 10 10 0 10 0 0 0 0 0 {too_large} 1 0 0 20 0 1 0 1 1 1")
        is None
    )


def _write_stat(proc_root, pid, pgrp, utime, stime, rss_pages, *, cutime=0, cstime=0) -> None:
    directory = proc_root / str(pid)
    directory.mkdir()
    (directory / "stat").write_text(
        f"{pid} (proc) R 1 {pgrp} {pgrp} 0 {pgrp} 0 0 0 0 0 "
        f"{utime} {stime} {cutime} {cstime} 20 0 1 0 999 123456 {rss_pages}"
    )


def test_read_process_group_usage_buckets_by_pgid(tmp_path) -> None:
    _write_stat(tmp_path, 10, 100, 5, 5, 50)
    _write_stat(tmp_path, 11, 100, 3, 2, 30)  # same group as pid 10
    _write_stat(tmp_path, 20, 200, 7, 1, 10)
    _write_stat(tmp_path, 30, 300, 9, 9, 99)  # not requested
    (tmp_path / "notapid").mkdir()  # ignored

    usage = sm.read_process_group_usage({100, 200}, proc_root=tmp_path, page_size=4096)

    assert usage[100] == ((5 + 5) + (3 + 2), (50 + 30) * 4096)
    assert usage[200] == (7 + 1, 10 * 4096)
    assert 300 not in usage  # unrequested group dropped
    assert sm.read_process_group_usage(set(), proc_root=tmp_path) == {}


def test_read_process_group_usage_drops_overflowing_group_totals(tmp_path) -> None:
    maximum = (1 << 64) - 1
    _write_stat(tmp_path, 10, 100, maximum, 0, 1)
    _write_stat(tmp_path, 11, 100, 1, 0, 1)  # CPU bucket overflow
    _write_stat(tmp_path, 20, 200, 1, 0, maximum // 4096 + 1)  # byte overflow
    _write_stat(tmp_path, 30, 300, 1, 0, 1)
    _write_stat(tmp_path, 40, 400, maximum, 1, 1)  # per-member CPU parse overflow
    _write_stat(tmp_path, 41, 400, 1, 0, 1)

    usage = sm.read_process_group_usage({100, 200, 300, 400}, proc_root=tmp_path, page_size=4096)

    assert set(usage) == {300}


def test_reaped_child_cpu_stays_in_process_group_counter(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    # Frame 1: child A is live. Frame 2: A has been waited for and its 10
    # jiffies moved into the leader's cutime while child B is live. The group
    # counter must advance 12 -> 15 instead of rolling back 12 -> 5.
    _write_stat(first_root, 10, 100, 2, 0, 10)
    _write_stat(first_root, 11, 100, 10, 0, 10)
    _write_stat(second_root, 10, 100, 3, 0, 10, cutime=10)
    _write_stat(second_root, 12, 100, 2, 0, 10)

    first = sm.read_process_group_usage({100}, proc_root=first_root, page_size=4096)
    second = sm.read_process_group_usage({100}, proc_root=second_root, page_size=4096)
    assert first[100][0] == 12
    assert second[100][0] == 15

    reads = iter([first, second])
    sampler = sm.ProcessGroupSampler(read_usage=lambda _pgids: next(reads), clk_tck=100)
    assert sampler.sample({"job": 100}, now=0.0)["job"].cpu_percent is None
    assert sampler.sample({"job": 100}, now=1.0)["job"].cpu_percent == 3.0


def test_process_group_sampler_delta_and_forgetting() -> None:
    reads = iter([{100: (1000, 4096)}, {100: (1000 + 400, 4096)}])
    sampler = sm.ProcessGroupSampler(read_usage=lambda pgids: next(reads), clk_tck=100)
    first = sampler.sample({"job": 100}, now=0.0)
    assert first["job"].cpu_percent is None and first["job"].rss_bytes == 4096
    # 400 jiffies / 100 tck = 4 cpu-seconds over 2 wall-seconds -> 200% (multi-core).
    second = sampler.sample({"job": 100}, now=2.0)
    assert second["job"].cpu_percent == 200.0
    # A group with no live member drops out entirely (fail closed).
    gone = sm.ProcessGroupSampler(read_usage=lambda pgids: {}, clk_tck=100)
    assert gone.sample({"job": 100}, now=0.0) == {}


def test_process_group_sampler_holds_high_watermark_after_counter_rollback() -> None:
    reads = iter(
        [
            {100: (1000, 4096)},
            {100: (100, 4096)},
            {100: (200, 4096)},
            {100: (1100, 4096)},
            {100: (1200, 4096)},
        ]
    )
    sampler = sm.ProcessGroupSampler(read_usage=lambda _pgids: next(reads), clk_tck=100)

    assert sampler.sample({"job": 100}, now=0.0)["job"].cpu_percent is None
    assert sampler.sample({"job": 100}, now=1.0)["job"].cpu_percent is None
    assert sampler.sample({"job": 100}, now=2.0)["job"].cpu_percent is None
    assert sampler.sample({"job": 100}, now=3.0)["job"].cpu_percent == pytest.approx(100 / 3)
    assert sampler.sample({"job": 100}, now=4.0)["job"].cpu_percent == 100.0


def test_process_group_sampler_keys_history_by_full_identity_and_clears_empty_frame() -> None:
    calls: list[set[int]] = []
    reads = iter(
        [
            {100: (1000, 4096)},
            {},
            {100: (1200, 8192)},
            {100: (1400, 8192)},
        ]
    )

    def _read(pgids) -> dict[int, tuple[int, int]]:
        calls.append(set(pgids))
        return next(reads)

    sampler = sm.ProcessGroupSampler(read_usage=_read, clk_tck=100)
    old_identity = ("boot-A", 100, 100, 10)
    new_identity = ("boot-A", 100, 100, 20)

    assert sampler.sample({old_identity: 100}, now=0.0)[old_identity].cpu_percent is None
    assert sampler.sample({}, now=1.0) == {}
    first_new = sampler.sample({new_identity: 100}, now=2.0)
    assert first_new[new_identity].cpu_percent is None
    second_new = sampler.sample({new_identity: 100}, now=3.0)
    assert second_new[new_identity].cpu_percent == 200.0
    assert calls == [{100}, set(), {100}, {100}]


def test_read_process_group_usage_finds_own_group_on_linux() -> None:
    from pathlib import Path

    if not Path("/proc/self/stat").exists():
        return  # non-Linux: /proc absent, covered by the fail-closed unit tests
    pgid = os.getpgrp()
    usage = sm.read_process_group_usage({pgid})
    assert pgid in usage
    cpu_jiffies, rss_bytes = usage[pgid]
    assert cpu_jiffies >= 0 and rss_bytes > 0


def test_real_proc_reads_are_wellformed_or_none() -> None:
    # On Linux these parse; on other platforms /proc is absent and each returns
    # None. Either way the readers must never raise.
    cpu = sm.read_cpu_times()
    assert cpu is None or (cpu.total > 0 and cpu.idle >= 0)
    mem = sm.read_meminfo()
    assert mem is None or (mem[1] > 0 and 0 <= mem[0] <= mem[1])
    load = sm.read_loadavg()
    assert load is None or len(load) == 3
