from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import (
    QueueEntry,
    QueueStatus,
    dequeue_entry_if_pending,
    enqueue,
    list_queue,
    request_cancel,
)
from orca_auto.xtb_md import queue_runtime
from orca_auto.xtb_md.engine import ENGINE_DEFINITION, build_worker_child_command


def _enqueue_entry(
    queue_root: Path,
    name: str,
    *,
    app_name: str = "orca_auto_xtb_md",
    engine: str = "xtb_md",
    task_kind: str = "xtb_md",
    priority: int = 10,
) -> QueueEntry:
    return enqueue(
        queue_root,
        app_name=app_name,
        task_id=f"task-{name}",
        task_kind=task_kind,
        engine=engine,
        priority=priority,
        metadata={"name": name},
    )


def _dequeue_exact(queue_root: Path, entry: QueueEntry) -> QueueEntry:
    running = dequeue_entry_if_pending(
        queue_root,
        entry.queue_id,
        expected_entry=entry,
    )
    assert running is not None
    assert running.status == QueueStatus.RUNNING
    return running


def _entry_by_task(queue_root: Path, task_id: str) -> QueueEntry:
    matches = [entry for entry in list_queue(queue_root) if entry.task_id == task_id]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    ("shutdown_requested", "rc", "expected_reason"),
    [
        (False, 9, "worker_child_exit_code=9"),
        (True, -15, "worker_shutdown_no_retry"),
    ],
)
def test_child_exit_is_terminal_failed_and_recovers_engine_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown_requested: bool,
    rc: int,
    expected_reason: str,
) -> None:
    queue_root = tmp_path / "queue"
    running = _dequeue_exact(queue_root, _enqueue_entry(queue_root, "child"))
    events: list[tuple[str, str]] = []

    monkeypatch.setattr(queue_runtime, "get_slot", lambda *_args: object())
    monkeypatch.setattr(
        queue_runtime,
        "recover_slot_engine_process",
        lambda _root, token: events.append(("recover", token)),
    )
    monkeypatch.setattr(
        queue_runtime,
        "persist_failed_job",
        lambda _cfg, _entry, *, reason: events.append(("persist", reason)),
    )
    real_mark_failed = queue_runtime.mark_failed

    def mark_failed(root: Path, queue_id: str, **kwargs: Any) -> QueueEntry | None:
        events.append(("mark", str(kwargs["error"])))
        return real_mark_failed(root, queue_id, **kwargs)

    monkeypatch.setattr(queue_runtime, "mark_failed", mark_failed)
    worker = SimpleNamespace(
        cfg=object(),
        admission_root=tmp_path / "admission",
        _shutdown_requested=shutdown_requested,
        _release_admission_slot=lambda token: events.append(("release", token)),
    )
    job = SimpleNamespace(
        queue_root=queue_root,
        entry=running,
        admission_token="slot-child",
    )

    queue_runtime._finalize_child_exit(worker, job, rc=rc)

    terminal = _entry_by_task(queue_root, running.task_id)
    assert terminal.status == QueueStatus.FAILED
    assert terminal.error == expected_reason
    assert [event[0] for event in events] == ["recover", "persist", "mark", "release"]
    assert events[0] == ("recover", "slot-child")
    assert events[-1] == ("release", "slot-child")
    assert all(entry.status != QueueStatus.PENDING for entry in list_queue(queue_root))


def test_cancel_requested_child_exit_is_terminal_cancelled_without_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root = tmp_path / "queue"
    running = _dequeue_exact(queue_root, _enqueue_entry(queue_root, "cancel"))
    requested = request_cancel(queue_root, running.queue_id, expected_entry=running)
    assert requested is not None and requested.cancel_requested
    events: list[tuple[str, str]] = []

    monkeypatch.setattr(queue_runtime, "get_slot", lambda *_args: object())
    monkeypatch.setattr(
        queue_runtime,
        "recover_slot_engine_process",
        lambda _root, token: events.append(("recover", token)),
    )
    monkeypatch.setattr(
        queue_runtime,
        "_persist_cancelled",
        lambda _cfg, _entry, *, reason: events.append(("persist_cancelled", reason)),
    )
    monkeypatch.setattr(
        queue_runtime,
        "persist_failed_job",
        lambda *_args, **_kwargs: pytest.fail("cancelled exit must not persist failure"),
    )
    worker = SimpleNamespace(
        cfg=object(),
        admission_root=tmp_path / "admission",
        _shutdown_requested=False,
        _release_admission_slot=lambda token: events.append(("release", token)),
    )

    queue_runtime._finalize_child_exit(
        worker,
        SimpleNamespace(
            queue_root=queue_root,
            entry=running,
            admission_token="slot-cancel",
        ),
        rc=-15,
    )

    terminal = _entry_by_task(queue_root, running.task_id)
    assert terminal.status == QueueStatus.CANCELLED
    assert terminal.error == "cancel_requested"
    assert events == [
        ("recover", "slot-cancel"),
        ("persist_cancelled", "cancel_requested"),
        ("release", "slot-cancel"),
    ]
    assert all(entry.status != QueueStatus.PENDING for entry in list_queue(queue_root))


def test_cancel_racing_failure_terminalization_wins_as_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root = tmp_path / "queue"
    running = _dequeue_exact(queue_root, _enqueue_entry(queue_root, "cancel-race"))
    real_mark_failed = queue_runtime.mark_failed

    def racing_mark_failed(root: Path, queue_id: str, **kwargs: Any) -> QueueEntry | None:
        current = _entry_by_task(queue_root, running.task_id)
        requested = request_cancel(root, queue_id, expected_entry=current)
        assert requested is not None and requested.cancel_requested
        return real_mark_failed(root, queue_id, **kwargs)

    monkeypatch.setattr(queue_runtime, "mark_failed", racing_mark_failed)
    monkeypatch.setattr(queue_runtime, "persist_failed_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(queue_runtime, "_persist_cancelled", lambda *_args, **_kwargs: None)

    queue_runtime._terminalize_abandoned(
        object(),
        queue_root,
        running,
        reason="worker_child_exit_code=9",
    )

    terminal = _entry_by_task(queue_root, running.task_id)
    assert terminal.status == QueueStatus.CANCELLED
    assert terminal.error == "cancel_requested"


def test_orphan_reconciliation_terminalizes_only_canonical_xtb_md_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root = tmp_path / "queue"
    failed = _dequeue_exact(queue_root, _enqueue_entry(queue_root, "orphan-failed"))
    cancelled = _dequeue_exact(queue_root, _enqueue_entry(queue_root, "orphan-cancelled"))
    requested = request_cancel(queue_root, cancelled.queue_id, expected_entry=cancelled)
    assert requested is not None and requested.cancel_requested
    foreign = _dequeue_exact(
        queue_root,
        _enqueue_entry(
            queue_root,
            "foreign-orca",
            app_name="orca_auto_orca",
            engine="orca",
            task_kind="orca_run_inp",
        ),
    )
    malformed = _dequeue_exact(
        queue_root,
        _enqueue_entry(
            queue_root,
            "wrong-app",
            app_name="orca_auto_orca",
            engine="xtb_md",
            task_kind="xtb_md",
        ),
    )
    persisted_failed: list[tuple[str, str]] = []
    persisted_cancelled: list[tuple[str, str]] = []
    reconciliation_events: list[str] = []

    monkeypatch.setattr(
        queue_runtime,
        "recover_orphaned_engine_slots",
        lambda *_args, **_kwargs: reconciliation_events.append("recover_engine_slots"),
    )
    monkeypatch.setattr(
        queue_runtime,
        "reconcile_stale_slots",
        lambda *_args, **_kwargs: reconciliation_events.append("reconcile_stale_slots"),
    )
    monkeypatch.setattr(queue_runtime, "list_slots", lambda _root: [])
    monkeypatch.setattr(queue_runtime, "runtime_roots_for_cfg", lambda _cfg: (queue_root,))
    monkeypatch.setattr(
        queue_runtime,
        "persist_failed_job",
        lambda _cfg, entry, *, reason: persisted_failed.append((entry.task_id, reason)),
    )
    monkeypatch.setattr(
        queue_runtime,
        "_persist_cancelled",
        lambda _cfg, entry, *, reason: persisted_cancelled.append((entry.task_id, reason)),
    )
    worker = SimpleNamespace(cfg=object(), admission_root=tmp_path / "admission")

    queue_runtime._reconcile_worker_state(worker)

    entries = {entry.task_id: entry for entry in list_queue(queue_root)}
    assert entries[failed.task_id].status == QueueStatus.FAILED
    assert entries[failed.task_id].error == "orphaned_worker_no_retry"
    assert entries[cancelled.task_id].status == QueueStatus.CANCELLED
    assert entries[cancelled.task_id].error == "cancel_requested"
    assert entries[foreign.task_id].status == QueueStatus.RUNNING
    assert entries[malformed.task_id].status == QueueStatus.RUNNING
    assert persisted_failed == [(failed.task_id, "orphaned_worker_no_retry")]
    assert persisted_cancelled == [(cancelled.task_id, "cancel_requested")]
    assert reconciliation_events == ["recover_engine_slots", "reconcile_stale_slots"]
    assert all(entries[task_id].status != QueueStatus.PENDING for task_id in entries)


def test_queue_definition_claims_only_complete_xtb_md_identity(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    foreign = _enqueue_entry(
        queue_root,
        "foreign",
        app_name="orca_auto_orca",
        engine="orca",
        task_kind="orca_run_inp",
        priority=0,
    )
    malformed = _enqueue_entry(
        queue_root,
        "malformed",
        app_name="orca_auto_orca",
        engine="xtb_md",
        task_kind="xtb_md",
        priority=0,
    )
    own = _enqueue_entry(queue_root, "own", priority=20)
    queue_functions = ENGINE_DEFINITION.queue_functions
    assert queue_functions is not None

    assert queue_functions.list_queue(queue_root) == [own]
    claimed = queue_functions.dequeue_next(queue_root)

    assert claimed is not None
    assert claimed.queue_id == own.queue_id
    statuses = {entry.queue_id: entry.status for entry in list_queue(queue_root)}
    assert statuses[own.queue_id] == QueueStatus.RUNNING
    assert statuses[foreign.queue_id] == QueueStatus.PENDING
    assert statuses[malformed.queue_id] == QueueStatus.PENDING


def test_parser_worker_child_command_and_worker_wiring(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    parsed = queue_runtime.build_parser().parse_args(["--config", str(config_path)])
    command = build_worker_child_command(
        config_path=str(config_path),
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
    )
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root=str(tmp_path / ".admission"),
            max_concurrent=0,
            admission_limit=4,
            engine_admission_limit=1,
        )
    )
    worker = queue_runtime.QueueWorker(cfg, str(config_path), max_concurrent=0)

    assert parsed.config == str(config_path)
    assert command[1:5] == [
        "-m",
        "orca_auto.core.engines.worker_child",
        "--engine",
        "xtb_md",
    ]
    assert command[-2:] == ["--admission-token", "slot-1"]
    assert worker.engine == "xtb_md"
    assert worker.max_concurrent == 1
    assert worker.worker_pid_file_name == "xtb_md_queue_worker.pid"
    assert worker.admission_root == (tmp_path / ".admission").resolve()
    assert worker._finalize_child_exit_callback is queue_runtime._finalize_child_exit
    assert worker._reconcile_orphaned_running_callback is queue_runtime._reconcile_worker_state


def test_cmd_queue_worker_loads_config_and_runs_xtb_md_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root=str(tmp_path / ".admission"),
            max_concurrent=3,
        )
    )
    seen: list[tuple[object, str, int | None]] = []

    class FakeWorker:
        def run(self) -> int:
            return 17

    def fake_worker(
        loaded_cfg: object,
        loaded_path: str,
        *,
        max_concurrent: int | None = None,
    ) -> FakeWorker:
        seen.append((loaded_cfg, loaded_path, max_concurrent))
        return FakeWorker()

    monkeypatch.setattr(queue_runtime, "load_config", lambda path: cfg if path else None)
    monkeypatch.setattr(queue_runtime, "QueueWorker", fake_worker)

    result = queue_runtime.cmd_queue_worker(
        queue_runtime.build_parser().parse_args(["--config", str(config_path)])
    )

    assert result == 17
    assert seen == [(cfg, str(config_path), 3)]


def _enqueue_with_lease(
    queue_root: Path,
    name: str,
    *,
    sync_state: str,
    owner_pid: int,
    owner_start: str | None = None,
    job_dir: Path | None = None,
) -> QueueEntry:
    from orca_auto.core.queue import (
        QUEUE_RECORD_SYNC_OWNER_START_KEY,
        queue_record_sync_metadata,
    )

    resolved_job_dir = job_dir if job_dir is not None else queue_root / f"job_{name}"
    resolved_job_dir.mkdir(parents=True, exist_ok=True)
    lease = queue_record_sync_metadata(sync_state, token=f"tok-{name}", owner_pid=owner_pid)
    if owner_start is not None:
        lease[QUEUE_RECORD_SYNC_OWNER_START_KEY] = owner_start
    return enqueue(
        queue_root,
        app_name="orca_auto_xtb_md",
        task_id=f"task-{name}",
        task_kind="xtb_md",
        engine="xtb_md",
        priority=10,
        metadata={"job_dir": str(resolved_job_dir), **lease},
    )


def _gate_worker(
    monkeypatch: pytest.MonkeyPatch,
    queue_root: Path,
) -> SimpleNamespace:
    monkeypatch.setattr(
        queue_runtime,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_root, entry) for entry in list_queue(queue_root)],
    )
    return SimpleNamespace(cfg=object())


def test_publication_repair_gate_repairs_stale_preparing_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The D6 crash window: a publisher SIGKILLed between the durable enqueue
    # commit and the queued-record publication leaves a dead-owner PREPARING
    # lease. Without the repair gate that row eventually becomes claimable and
    # runs without any published record.
    from orca_auto.core.queue import (
        QUEUE_RECORD_SYNC_COMPLETE,
        QUEUE_RECORD_SYNC_PREPARING,
        queue_entry_is_claimable,
        queue_record_sync_state,
    )

    queue_root = tmp_path / "queue"
    entry = _enqueue_with_lease(
        queue_root,
        "sigkilled",
        sync_state=QUEUE_RECORD_SYNC_PREPARING,
        owner_pid=2**22 + 12345,
        owner_start="dead-publisher-start-token",
    )
    # This is the hazard: the dead-owner lease is already stale, so the generic
    # dequeue would claim the row even though no queued record was published.
    assert queue_entry_is_claimable(_entry_by_task(queue_root, entry.task_id))

    published: list[str] = []
    monkeypatch.setattr(
        queue_runtime,
        "publish_queued_record",
        lambda _cfg, row: published.append(row.queue_id),
    )
    worker = _gate_worker(monkeypatch, queue_root)

    assert queue_runtime._publication_repair_gate(worker) is None

    repaired = _entry_by_task(queue_root, entry.task_id)
    assert published == [entry.queue_id]
    assert repaired.status == QueueStatus.PENDING
    assert queue_record_sync_state(repaired) == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(repaired)


def test_publication_repair_gate_blocks_and_parks_when_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.queue import (
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        queue_entry_is_claimable,
        queue_record_sync_state,
    )

    queue_root = tmp_path / "queue"
    entry = _enqueue_with_lease(
        queue_root,
        "broken",
        sync_state=QUEUE_RECORD_SYNC_REPAIR_PENDING,
        owner_pid=0,
    )

    def explode(_cfg: object, _row: object) -> None:
        raise RuntimeError("artifact write failed")

    monkeypatch.setattr(queue_runtime, "publish_queued_record", explode)
    worker = _gate_worker(monkeypatch, queue_root)

    assert queue_runtime._publication_repair_gate(worker) == ("blocked", None)

    parked = _entry_by_task(queue_root, entry.task_id)
    assert parked.status == QueueStatus.PENDING
    assert queue_record_sync_state(parked) == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert not queue_entry_is_claimable(parked)


def test_publication_repair_gate_leaves_live_preparing_lease_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.queue import (
        QUEUE_RECORD_SYNC_PREPARING,
        queue_record_sync_state,
    )

    queue_root = tmp_path / "queue"
    entry = _enqueue_with_lease(
        queue_root,
        "live",
        sync_state=QUEUE_RECORD_SYNC_PREPARING,
        owner_pid=os.getpid(),
    )

    monkeypatch.setattr(
        queue_runtime,
        "publish_queued_record",
        lambda _cfg, _row: pytest.fail("a live publisher lease must not be repaired"),
    )
    worker = _gate_worker(monkeypatch, queue_root)

    assert queue_runtime._publication_repair_gate(worker) is None
    current = _entry_by_task(queue_root, entry.task_id)
    assert queue_record_sync_state(current) == QUEUE_RECORD_SYNC_PREPARING


def test_publication_repair_gate_refuses_job_dir_outside_queue_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.queue import QUEUE_RECORD_SYNC_REPAIR_PENDING

    queue_root = tmp_path / "queue"
    outside = tmp_path / "outside_job"
    _enqueue_with_lease(
        queue_root,
        "escape",
        sync_state=QUEUE_RECORD_SYNC_REPAIR_PENDING,
        owner_pid=0,
        job_dir=outside,
    )

    monkeypatch.setattr(
        queue_runtime,
        "publish_queued_record",
        lambda _cfg, _row: pytest.fail("an escaping job_dir must not be published"),
    )
    worker = _gate_worker(monkeypatch, queue_root)

    assert queue_runtime._publication_repair_gate(worker) == ("blocked", None)


def test_queue_worker_wires_the_publication_repair_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_builder(cfg: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "worker"

    monkeypatch.setattr(queue_runtime, "build_runtime_engine_queue_worker", fake_builder)
    monkeypatch.setattr(queue_runtime, "resolve_admission_root", lambda _cfg: "/tmp/admission")

    assert queue_runtime.QueueWorker(object(), "/tmp/orca_auto.yaml") == "worker"
    assert captured["reserve_gate"] is queue_runtime._publication_repair_gate


def test_dequeue_refuses_unpublished_sync_lease_even_when_claimable(tmp_path: Path) -> None:
    # Closes the residual window: if the publisher dies between the repair
    # gate's scan (which saw a live lease) and the claim, the stale lease is
    # claimable by the generic rule but the xTB-MD claim itself must refuse
    # every unfinished publication.
    from orca_auto.core.queue import (
        QUEUE_RECORD_SYNC_COMPLETE,
        QUEUE_RECORD_SYNC_PREPARING,
        queue_entry_is_claimable,
        queue_record_sync_metadata,
    )
    from orca_auto.xtb_md.engine import _dequeue_next_xtb_md

    queue_root = tmp_path / "queue"
    stale = _enqueue_with_lease(
        queue_root,
        "stale-window",
        sync_state=QUEUE_RECORD_SYNC_PREPARING,
        owner_pid=2**22 + 54321,
        owner_start="dead-publisher-start-token",
    )
    assert queue_entry_is_claimable(_entry_by_task(queue_root, stale.task_id))

    assert _dequeue_next_xtb_md(queue_root) is None

    def publish_lease(entries: list[Any]) -> tuple[None, bool]:
        from dataclasses import replace as dc_replace

        for index, row in enumerate(entries):
            if row.task_id != stale.task_id:
                continue
            metadata = dict(row.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE, token="tok-stale-window", owner_pid=0
                )
            )
            entries[index] = dc_replace(row, metadata=metadata)
            return None, True
        raise AssertionError("row disappeared")

    from orca_auto.core.queue.store import mutate_entries

    mutate_entries(queue_root, publish_lease)
    claimed = _dequeue_next_xtb_md(queue_root)
    assert claimed is not None and claimed.task_id == stale.task_id


def test_publication_repair_gate_treats_cancel_fenced_row_as_settled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.queue import (
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        request_cancel,
    )

    queue_root = tmp_path / "queue"
    entry = _enqueue_with_lease(
        queue_root,
        "cancel-fenced",
        sync_state=QUEUE_RECORD_SYNC_REPAIR_PENDING,
        owner_pid=0,
    )
    cancelled = request_cancel(queue_root, entry.queue_id, expected_entry=entry)
    assert cancelled is not None and cancelled.cancel_requested

    monkeypatch.setattr(
        queue_runtime,
        "publish_queued_record",
        lambda _cfg, _row: pytest.fail("a cancel-fenced row must not be published"),
    )
    worker = _gate_worker(monkeypatch, queue_root)

    assert queue_runtime._publication_repair_gate(worker) is None


def test_publication_repair_gate_blocks_on_invalid_sync_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.queue import QUEUE_RECORD_SYNC_ABORTED

    queue_root = tmp_path / "queue"
    _enqueue_with_lease(
        queue_root,
        "aborted",
        sync_state=QUEUE_RECORD_SYNC_ABORTED,
        owner_pid=0,
    )

    monkeypatch.setattr(
        queue_runtime,
        "publish_queued_record",
        lambda _cfg, _row: pytest.fail("an aborted lease must not be published"),
    )
    worker = _gate_worker(monkeypatch, queue_root)

    assert queue_runtime._publication_repair_gate(worker) == ("blocked", None)
