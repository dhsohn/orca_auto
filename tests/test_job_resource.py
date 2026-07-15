from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto import job_resource
from orca_auto.job_resource import JobProcessIdentity, LiveJobMetricsSampler
from orca_auto.system_metrics import JobMetrics, ProcessGroupSampler


def _slot(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "queue_id": "q",
        "engine_pid": 100,
        "engine_pgid": 100,
        "engine_process_start_ticks": 555,
        "engine_process_boot_id": "boot-A",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _resolve(slots: list[SimpleNamespace], **overrides: Any) -> list[JobProcessIdentity]:
    defaults: dict[str, Any] = {
        "engine_runtime_paths_fn": lambda cfg, engine: {"admission_root": Path("/x")},
        "read_slots_fn": lambda root: slots,
        "is_alive": lambda pid: True,
        "start_ticks": lambda pid: None,
        "boot_id": lambda: "boot-A",
    }
    defaults.update(overrides)
    return job_resource.live_job_processes("orca_auto.yaml", **defaults)


def test_live_job_processes_returns_validated_live_identities() -> None:
    slots = [
        _slot(queue_id="q1", engine_pid=111, engine_pgid=111, engine_process_start_ticks=555),
        _slot(queue_id="q2", engine_pid=222, engine_pgid=222, engine_process_start_ticks=777),
    ]
    result = _resolve(slots, start_ticks=lambda pid: {111: 555, 222: 777}[pid])
    assert result == [
        JobProcessIdentity("q1", 111, 111, 555, "boot-A"),
        JobProcessIdentity("q2", 222, 222, 777, "boot-A"),
    ]


def test_live_job_processes_drops_stale_reused_and_incomplete() -> None:
    slots = [
        _slot(queue_id="dead", engine_pid=1),  # not alive
        _slot(queue_id="otherboot", engine_process_boot_id="boot-OLD"),  # recorded on old boot
        _slot(queue_id="reused", engine_pid=300, engine_process_start_ticks=999),  # ticks mismatch
        _slot(queue_id="nopid", engine_pid=None),
        _slot(queue_id="nopgid", engine_pgid=None),
        _slot(queue_id="", engine_pgid=100),  # missing queue id
        _slot(queue_id="live", engine_pid=444, engine_pgid=444, engine_process_start_ticks=42),
    ]
    result = _resolve(
        slots,
        is_alive=lambda pid: pid != 1,
        start_ticks=lambda pid: {300: 111, 444: 42}.get(pid),
        boot_id=lambda: "boot-A",
    )
    assert result == [JobProcessIdentity("live", 444, 444, 42, "boot-A")]


def test_live_job_processes_fails_closed_on_missing_config_or_root() -> None:
    assert job_resource.live_job_processes(None) == []
    assert job_resource.live_job_processes("   ") == []
    assert (
        job_resource.live_job_processes(
            "orca_auto.yaml",
            engine_runtime_paths_fn=lambda cfg, engine: {},  # no admission_root
            read_slots_fn=lambda root: [],
        )
        == []
    )


def test_live_job_processes_fails_closed_when_slot_store_raises() -> None:
    def _boom(root: Any) -> list[Any]:
        raise OSError("corrupt")

    assert _resolve([], read_slots_fn=_boom) == []


def test_live_job_processes_requires_exact_boot_identity() -> None:
    slot = _slot()
    assert _resolve([slot], start_ticks=lambda _pid: 555, boot_id=lambda: None) == []
    assert _resolve([slot], start_ticks=lambda _pid: 555, boot_id=lambda: "   ") == []
    assert _resolve([slot], start_ticks=lambda _pid: 555, boot_id=lambda: "boot-B") == []
    assert _resolve([_slot(engine_process_boot_id=None)], start_ticks=lambda _pid: 555) == []


def test_live_job_processes_drops_ambiguous_queue_ids_and_pgids() -> None:
    slots = [
        _slot(queue_id="duplicate-queue", engine_pid=100, engine_pgid=100),
        _slot(queue_id="duplicate-queue", engine_pid=200, engine_pgid=200),
        _slot(queue_id="first-pgid", engine_pid=300, engine_pgid=300),
        _slot(queue_id="second-pgid", engine_pid=300, engine_pgid=300),
        _slot(queue_id="unique", engine_pid=400, engine_pgid=400),
    ]
    assert _resolve(slots, start_ticks=lambda _pid: 555) == [
        JobProcessIdentity("unique", 400, 400, 555, "boot-A")
    ]


def test_read_slots_default_is_read_only(tmp_path: Path) -> None:
    # Regression: the metrics view must observe admission slots without rewriting
    # or pruning the worker's durable state (the writing `list_slots` would drop a
    # dead-owner slot and rewrite the file on every read).
    from orca_auto.core.admission import read_active_slot_count, read_slots
    from orca_auto.core.admission.persistence import ADMISSION_FILE_NAME
    from orca_auto.core.admission.records import AdmissionSlot
    from orca_auto.core.admission.store import _save_slots

    dead_owner_slot = AdmissionSlot(
        token="t1",
        owner_pid=2_000_000_000,  # not a live pid
        process_start_ticks=1,
        source="orca_auto",
        acquired_at="2026-01-01T00:00:00Z",
        queue_id="q1",
    )
    _save_slots(tmp_path, [dead_owner_slot])
    path = tmp_path / ADMISSION_FILE_NAME
    before = path.read_bytes()

    slots = read_slots(tmp_path)

    assert path.read_bytes() == before  # not rewritten
    assert [slot.token for slot in slots] == ["t1"]  # dead-owner slot not pruned
    assert read_active_slot_count(tmp_path) == 0
    assert path.read_bytes() == before  # passive active count also leaves it in place


def test_live_job_metrics_sampler_clears_empty_and_revalidates_identity() -> None:
    old = JobProcessIdentity("old", 100, 100, 10, "boot-A")
    new = JobProcessIdentity("new", 100, 100, 20, "boot-A")
    live_reads = iter(([old], [old], [], [new], [new]))
    calls: list[dict[JobProcessIdentity, int]] = []

    class _ProbeSampler:
        def sample(self, targets, *, now: float):
            del now
            calls.append(dict(targets))
            return {identity: JobMetrics(cpu_percent=None, rss_bytes=1) for identity in targets}

        def retain_identities(self, identities) -> None:
            del identities

    sampler = LiveJobMetricsSampler(
        process_sampler=_ProbeSampler(),  # type: ignore[arg-type]
        clock=lambda: 1.0,
        live_processes_fn=lambda _config: next(live_reads),
    )

    assert set(sampler.sample("config")) == {"old"}
    assert sampler.sample("config") == {}
    assert set(sampler.sample("config")) == {"new"}
    assert calls == [{old: 100}, {}, {new: 100}]


def test_live_job_metrics_sampler_drops_identity_changed_during_scan() -> None:
    old = JobProcessIdentity("q", 100, 100, 10, "boot-A")
    reused = JobProcessIdentity("q", 100, 100, 20, "boot-A")
    live_reads = iter(([old], [reused]))

    class _ProbeSampler:
        def sample(self, targets, *, now: float):
            del now
            return {identity: JobMetrics(cpu_percent=100.0, rss_bytes=1) for identity in targets}

        def retain_identities(self, identities) -> None:
            del identities

    sampler = LiveJobMetricsSampler(
        process_sampler=_ProbeSampler(),  # type: ignore[arg-type]
        live_processes_fn=lambda _config: next(live_reads),
    )

    assert sampler.sample("config") == {}


def test_live_job_metrics_sampler_discards_unpublished_delta_baseline() -> None:
    identity = JobProcessIdentity("q", 100, 100, 10, "boot-A")
    live_reads = iter(([identity], [], [identity], [identity]))
    usage_reads = iter(({100: (100, 1)}, {100: (300, 1)}))
    clock_reads = iter((0.0, 1.0))
    process_sampler = ProcessGroupSampler(
        read_usage=lambda _pgids: next(usage_reads),
        clk_tck=100,
    )
    sampler = LiveJobMetricsSampler(
        process_sampler=process_sampler,
        clock=lambda: next(clock_reads),
        live_processes_fn=lambda _config: next(live_reads),
    )

    assert sampler.sample("config") == {}
    recovered = sampler.sample("config")
    assert recovered["q"].cpu_percent is None
