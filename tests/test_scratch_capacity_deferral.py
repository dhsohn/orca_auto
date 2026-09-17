"""A RAM scratch capacity refusal before launch leaves the job queued, not failed."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.core.config import CommonResourceConfig
from orca_auto.core.engine_scratch import (
    EngineScratchCapacityError,
    EngineScratchWorkspace,
)
from orca_auto.core.queue.deferral import (
    ADMISSION_DEFERRAL_INTERVAL_SECONDS,
    ADMISSION_DEFERRAL_METADATA_KEY,
    admission_deferral_update,
    queue_entry_admission_deferral_reason,
    queue_entry_admission_is_deferred,
)
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.admission import peek_next_across_roots
from orca_auto.orca import execution as run_inp_execution
from orca_auto.orca import worker_execution as worker_job
from orca_auto.orca.execution_binding import (
    build_orca_execution_snapshot,
    orca_execution_started_evidence,
)
from orca_auto.orca.orca_runner import OrcaRunner, RunResult
from orca_auto.orca.queue.adapter import (
    dequeue_next,
    enqueue,
    list_queue,
    queue_entries_same_publication_generation,
)
from orca_auto.orca.run_context import RunExecutionContext
from orca_auto.orca.state_reading import load_state, state_path

_REFUSAL = "engine scratch cannot guarantee RAM headroom without swap: available_memory=1"


# --- the deferral record -----------------------------------------------------------------


def _entry(metadata: dict[str, Any], *, status: QueueStatus = QueueStatus.PENDING) -> QueueEntry:
    return QueueEntry(
        queue_id="q-1",
        app_name="orca_auto_orca",
        task_id="task-1",
        task_kind="orca_run_inp",
        engine="orca",
        status=status,
        metadata=metadata,
    )


def test_deferral_holds_a_row_for_one_interval_only() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entry = _entry(admission_deferral_update(_REFUSAL, now=now))

    assert queue_entry_admission_deferral_reason(entry) == _REFUSAL
    assert queue_entry_admission_is_deferred(entry, now=now)
    just_before = now + timedelta(seconds=ADMISSION_DEFERRAL_INTERVAL_SECONDS - 1)
    assert queue_entry_admission_is_deferred(entry, now=just_before)
    due = now + timedelta(seconds=ADMISSION_DEFERRAL_INTERVAL_SECONDS)
    assert not queue_entry_admission_is_deferred(entry, now=due)


def test_deferral_written_against_a_later_clock_cannot_park_the_row() -> None:
    # WSL2 steps the wall clock backwards; a wait longer than one interval was
    # not written against the current clock.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entry = _entry(admission_deferral_update(_REFUSAL, now=now + timedelta(hours=3)))

    assert not queue_entry_admission_is_deferred(entry, now=now)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {ADMISSION_DEFERRAL_METADATA_KEY: "soon"},
        {ADMISSION_DEFERRAL_METADATA_KEY: {"reason": _REFUSAL}},
        {ADMISSION_DEFERRAL_METADATA_KEY: {"reason": _REFUSAL, "not_before": "not a time"}},
    ],
)
def test_missing_or_malformed_deferral_never_blocks_a_claim(metadata: dict[str, Any]) -> None:
    assert not queue_entry_admission_is_deferred(_entry(metadata))


def test_deferral_is_lifecycle_metadata_not_generation_identity() -> None:
    plain = _entry({"reaction_dir": "/runs/rxn"})
    deferred = _entry({"reaction_dir": "/runs/rxn", **admission_deferral_update(_REFUSAL)})

    assert queue_entry_generation_token(deferred) == queue_entry_generation_token(plain)
    assert queue_entries_same_publication_generation(deferred, plain)


# --- claiming -------------------------------------------------------------------------------


def _bound_orca_metadata(tmp_path: Path, reaction_dir: Path) -> dict[str, Any]:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    selected = reaction_dir / "job.inp"
    selected.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    executable = tmp_path / "fake-orca"
    if not executable.exists():
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    resources = {"max_cores": 1, "max_memory_gb": 1}
    snapshot = build_orca_execution_snapshot(
        reaction_dir,
        selected,
        selected_input_xyz="",
        resource_request=resources,
        orca_executable=executable,
    )
    return {
        "reaction_dir": str(reaction_dir),
        "force": True,
        "source_selected_inp": str(selected),
        "selected_inp": snapshot["selected_inp"],
        "selected_input_xyz": "",
        "resource_request": resources,
        "execution_snapshot": snapshot,
    }


def _child_cfg(queue_root: Path, admission_root: Path) -> Any:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(queue_root),
            admission_root=str(admission_root),
            admission_limit=1,
            max_concurrent=1,
            resolved_admission_root=str(admission_root),
            resolved_admission_limit=1,
        ),
        resources=CommonResourceConfig(),
    )


def _run_child_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    queue_root: Path,
    queue_id: str,
    execute: Any,
) -> int:
    monkeypatch.setattr(
        worker_job, "load_config", lambda _path: _child_cfg(queue_root, tmp_path / "admission")
    )
    monkeypatch.setattr(worker_job, "install_shutdown_signal_handlers", lambda _callback: None)
    monkeypatch.setattr(worker_job, "execute_run_job", execute)
    return worker_job.run_worker_child_job(
        config_path="/tmp/config.yaml",
        queue_root=queue_root,
        queue_id=queue_id,
        admission_token="slot-deferral",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )


def _refuse(*_args: Any, **_kwargs: Any) -> int:
    raise EngineScratchCapacityError(_REFUSAL)


def _generation_listing(reaction_dir: Path) -> list[str]:
    return sorted(str(path.relative_to(reaction_dir)) for path in reaction_dir.rglob("*"))


def test_capacity_refusal_returns_the_row_to_the_queue_without_touching_its_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    rxn = queue_root / "rxn"
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-deferred",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    running = dequeue_next(queue_root)
    assert running is not None
    listing_before = _generation_listing(rxn)

    rc = _run_child_with(monkeypatch, tmp_path, queue_root, entry.queue_id, _refuse)

    # Non-zero on purpose: had the requeue been fenced out, the parent must
    # mark the still-running row failed, never completed.
    assert rc == worker_job.ADMISSION_DEFERRED_EXIT_CODE != 0
    [deferred] = list_queue(queue_root)
    assert deferred.status == QueueStatus.PENDING
    assert deferred.started_at == ""
    assert deferred.error == ""
    assert queue_entry_admission_deferral_reason(deferred) == _REFUSAL
    assert queue_entry_generation_token(deferred) == queue_entry_generation_token(running)
    assert queue_entries_same_publication_generation(deferred, running)
    # Nothing ran: the generation is pristine, so the next claim reuses it
    # instead of spending the bounded crash-recovery rebind budget.
    assert _generation_listing(rxn) == listing_before
    assert not orca_execution_started_evidence(rxn, deferred.metadata["execution_snapshot"])
    assert worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY not in deferred.metadata


def test_deferred_row_is_skipped_until_due_and_does_not_block_the_row_behind_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    big = enqueue(
        queue_root,
        str(queue_root / "big"),
        force=True,
        task_id="task-big",
        metadata=_bound_orca_metadata(tmp_path, queue_root / "big"),
    )
    small = enqueue(
        queue_root,
        str(queue_root / "small"),
        force=True,
        task_id="task-small",
        metadata=_bound_orca_metadata(tmp_path, queue_root / "small"),
    )
    assert dequeue_next(queue_root) is not None
    _run_child_with(monkeypatch, tmp_path, queue_root, big.queue_id, _refuse)

    def peek(accept_entry_fn: Any) -> Any:
        # None takes the single-root fast path; ORCA workers pass a filter.
        return peek_next_across_roots(
            (queue_root,),
            list_queue_fn=list_queue,
            select_all_rows=True,
            accept_entry_fn=accept_entry_fn,
        )

    for accept_entry_fn in (None, lambda _entry: True):
        peeked = peek(accept_entry_fn)
        assert peeked is not None and peeked[1].queue_id == small.queue_id
    claimed = dequeue_next(queue_root)
    assert claimed is not None and claimed.queue_id == small.queue_id
    # Only the deferred row is left: the worker sees an idle queue before it
    # reserves a slot, instead of respawning the row on every poll.
    assert dequeue_next(queue_root) is None
    for accept_entry_fn in (None, lambda _entry: True):
        assert peek(accept_entry_fn) is None

    import orca_auto.core.queue.store as store_mod

    monkeypatch.setattr(store_mod, "queue_entry_admission_is_deferred", lambda _entry: False)
    due = dequeue_next(queue_root)
    assert due is not None and due.queue_id == big.queue_id


def test_claim_removes_the_deferral_so_a_later_requeue_is_not_mislabelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orca_auto.orca.queue.adapter import requeue_running_entry

    queue_root = tmp_path / "queue"
    rxn = queue_root / "rxn"
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-relabel",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    first_claim = dequeue_next(queue_root)
    assert first_claim is not None
    _run_child_with(monkeypatch, tmp_path, queue_root, entry.queue_id, _refuse)
    import orca_auto.core.queue.store as store_mod

    monkeypatch.setattr(store_mod, "queue_entry_admission_is_deferred", lambda _entry: False)

    reclaimed = dequeue_next(queue_root)

    assert reclaimed is not None
    assert ADMISSION_DEFERRAL_METADATA_KEY not in reclaimed.metadata
    assert queue_entry_generation_token(reclaimed) == queue_entry_generation_token(first_claim)
    # ORCA started this time and the worker was then shut down: the row is
    # pending again, but no longer because it waits for resources.
    assert requeue_running_entry(queue_root, entry.queue_id, expected_entry=reclaimed)
    [requeued] = list_queue(queue_root)
    assert requeued.status == QueueStatus.PENDING
    assert queue_entry_admission_deferral_reason(requeued) == ""


def test_cancel_requested_while_deferring_wins_over_the_requeue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orca_auto.orca.queue.adapter import cancel

    queue_root = tmp_path / "queue"
    rxn = queue_root / "rxn"
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-cancel",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    assert dequeue_next(queue_root) is not None
    cancel(queue_root, entry.queue_id)

    rc = _run_child_with(monkeypatch, tmp_path, queue_root, entry.queue_id, _refuse)

    assert rc == worker_job.ADMISSION_DEFERRED_EXIT_CODE
    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert queue_entry_admission_deferral_reason(cancelled) == ""


def test_other_scratch_failures_still_fail_the_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    rxn = queue_root / "rxn"
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-unsafe",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    assert dequeue_next(queue_root) is not None

    rc = _run_child_with(monkeypatch, tmp_path, queue_root, entry.queue_id, lambda *_a, **_k: 1)

    assert rc == 1
    [row] = list_queue(queue_root)
    assert row.status == QueueStatus.RUNNING  # the parent finalizer marks it failed from rc=1
    assert queue_entry_admission_deferral_reason(row) == ""


# --- the run itself -------------------------------------------------------------------------


class _ManagedRunner(OrcaRunner):
    launches: list[Path] = []

    def __init__(self, orca_executable: str) -> None:
        super().__init__(orca_executable)
        self.set_running_job_registrar(lambda _job: None, prepare=lambda: None)

    def _run_in_place(
        self, inp_path: Path, *, working_directory_fd: int | None = None
    ) -> RunResult:
        assert working_directory_fd is not None
        type(self).launches.append(inp_path)
        out = inp_path.with_suffix(".out")
        out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
        return RunResult(out_path=str(out), return_code=0)


def _scratch_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    available_memory_bytes: int,
) -> tuple[Path, Path, list[Any], Any]:
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(
        scratch_mod, "_linux_available_memory_bytes", lambda: available_memory_bytes
    )
    monkeypatch.setattr(scratch_mod, "_filesystem_free_bytes", lambda _descriptor: 2 * 1024**3)
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    notifications: list[Any] = []

    @contextmanager
    def passthrough(*_args: object, **_kwargs: object):
        yield

    monkeypatch.setattr(run_inp_execution, "acquire_run_lock", passthrough)
    monkeypatch.setattr(run_inp_execution, "_admission_context", passthrough)
    monkeypatch.setattr(
        run_inp_execution,
        "notification_callbacks",
        lambda _cfg: (notifications.append, notifications.append),
    )
    _ManagedRunner.launches = []
    context = cast(
        RunExecutionContext,
        SimpleNamespace(
            reaction_dir=reaction_dir,
            selected_inp=inp,
            admission_root=None,
            reservation_token=None,
            admission_app_name=None,
            admission_task_id="",
            cfg=SimpleNamespace(
                paths=SimpleNamespace(orca_executable="/bin/true"),
                scratch=SimpleNamespace(enabled=True, root=str(shm / "orca_auto"), min_free_gb=1),
                resources=SimpleNamespace(max_memory_gb_per_task=1),
            ),
        ),
    )
    return reaction_dir, shm / "orca_auto", notifications, context


def test_capacity_refusal_before_launch_writes_no_state_and_sends_no_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir, scratch_root, notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=1
    )
    listing_before = _generation_listing(reaction_dir)

    with pytest.raises(EngineScratchCapacityError, match="RAM headroom"):
        run_inp_execution.execute_locked_run(
            SimpleNamespace(force=False), context, runner_cls=_ManagedRunner
        )

    assert _generation_listing(reaction_dir) == listing_before
    assert not state_path(reaction_dir).exists()
    assert notifications == []
    assert _ManagedRunner.launches == []
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_execute_orca_run_does_not_turn_the_refusal_into_an_ordinary_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _reaction_dir, _root, _notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=1
    )
    monkeypatch.setattr(run_inp_execution, "resolve_execution_context", lambda *_a, **_k: context)

    with pytest.raises(EngineScratchCapacityError):
        run_inp_execution.execute_orca_run(SimpleNamespace(force=False), runner_cls=_ManagedRunner)


def test_admitted_run_launches_in_the_workspace_it_reserved_before_writing_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir, scratch_root, _notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=2**63
    )
    created: list[bool] = []
    real_create = EngineScratchWorkspace.create.__func__  # type: ignore[attr-defined]

    def counting_create(cls: Any, *args: Any, **kwargs: Any) -> Any:
        created.append(state_path(reaction_dir).exists())
        return real_create(cls, *args, **kwargs)

    monkeypatch.setattr(EngineScratchWorkspace, "create", classmethod(counting_create))

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=False), context, runner_cls=_ManagedRunner
    )

    assert exit_code == 0
    assert created == [False]  # one workspace, reserved before the first state write
    assert len(_ManagedRunner.launches) == 1
    state = load_state(reaction_dir)
    assert state is not None and state["status"] == "completed"
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_reserved_workspace_is_removed_when_the_run_fails_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A workspace left behind by a live owner that then exits would make every
    # later scratch attempt fail closed as stale.
    _reaction_dir, scratch_root, _notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=2**63
    )

    def unreadable_state(*_args: Any, **_kwargs: Any) -> Any:
        assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")]
        raise OSError("state directory is unreadable")

    monkeypatch.setattr(run_inp_execution, "load_or_create_state", unreadable_state)

    with pytest.raises(OSError, match="unreadable"):
        run_inp_execution.execute_locked_run(
            SimpleNamespace(force=False), context, runner_cls=_ManagedRunner
        )

    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []
    assert _ManagedRunner.launches == []


def test_workspace_reserved_for_another_input_is_never_used_to_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir, scratch_root, _notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=2**63
    )
    derived = reaction_dir / "rxn_resume.inp"
    derived.write_text("! SP\n* xyz 0 1\nHe 0 0 0\n*\n", encoding="utf-8")
    runner = _ManagedRunner("/bin/true")
    runner.set_scratch_policy(
        run_inp_execution.OrcaScratchPolicy(
            root=scratch_root, min_free_bytes=1024**3, max_task_memory_bytes=1024**3
        )
    )

    runner.prepare(context.selected_inp)
    result = runner.run(derived)

    assert [path.name for path in _ManagedRunner.launches] == ["rxn_resume.inp"]
    assert Path(result.out_path) == derived.with_suffix(".out")
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_capacity_refusal_after_the_run_started_is_a_failed_attempt_not_a_deferral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Once state and the started notification exist, the job has started:
    # waiting again would be an automatic rerun of a recorded attempt.
    reaction_dir, _root, notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=2**63
    )

    class RefusedAtLaunch(_ManagedRunner):
        def prepare(self, inp_path: Path) -> None:
            return None

        def run(self, inp_path: Path) -> RunResult:
            raise EngineScratchCapacityError(_REFUSAL)

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=False), context, runner_cls=RefusedAtLaunch
    )

    assert exit_code == 1
    state = load_state(reaction_dir)
    assert state is not None and state["status"] == "failed"
    final_result = state["final_result"]
    assert final_result is not None and final_result["reason"] == "runner_exception"
    assert len(notifications) == 2


def test_unsafe_scratch_root_still_fails_the_attempt_with_its_state_and_notifications(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir, scratch_root, notifications, context = _scratch_run(
        monkeypatch, tmp_path, available_memory_bytes=2**63
    )
    scratch_root.mkdir()
    (scratch_root / "attempt-unknown").mkdir()  # no manifest: ownership cannot be verified

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=False), context, runner_cls=_ManagedRunner
    )

    assert exit_code == 1
    state = load_state(reaction_dir)
    assert state is not None and state["status"] == "failed"
    final_result = state["final_result"]
    assert final_result is not None and final_result["reason"] == "runner_exception"
    assert len(notifications) == 2
    assert _ManagedRunner.launches == []


# --- the whole worker child, with the real runner and a real admission slot ---------------


def test_worker_child_defers_a_real_run_and_the_next_claim_reuses_the_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orca_auto.core.admission import list_slots
    from orca_auto.core.queue.worker.admission import reserve_engine_queue_worker_slot

    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(scratch_mod, "_filesystem_free_bytes", lambda _descriptor: 2 * 1024**3)
    available = {"bytes": 1}
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: available["bytes"])
    notifications: list[Any] = []
    monkeypatch.setattr(
        run_inp_execution,
        "notification_callbacks",
        lambda _cfg: (notifications.append, notifications.append),
    )

    queue_root = tmp_path / "queue"
    admission_root = tmp_path / "admission"
    rxn = queue_root / "rxn"
    fake_orca = tmp_path / "fake-orca"
    fake_orca.write_text(
        "#!/bin/sh\necho '****ORCA TERMINATED NORMALLY****'\nexit 0\n", encoding="utf-8"
    )
    fake_orca.chmod(0o755)
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-real-deferral",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    cfg = _child_cfg(queue_root, admission_root)
    cfg.paths = SimpleNamespace(orca_executable=str(tmp_path / "fake-orca"))
    cfg.scratch = SimpleNamespace(enabled=True, root=str(shm / "orca_auto"), min_free_gb=1)
    monkeypatch.setattr(worker_job, "load_config", lambda _path: cfg)
    monkeypatch.setattr(worker_job, "install_shutdown_signal_handlers", lambda _callback: None)

    def run_child() -> int:
        token = reserve_engine_queue_worker_slot(cfg, engine="orca")
        assert token is not None
        return worker_job.run_worker_child_job(
            config_path="/tmp/config.yaml",
            queue_root=queue_root,
            queue_id=entry.queue_id,
            admission_token=token,
            await_parent_admission_handoff_fn=lambda *_args: True,
        )

    running = dequeue_next(queue_root)
    assert running is not None
    # The job root keeps its run.lock file as after any run; started-execution
    # evidence is judged on the generation directory alone.
    generation = Path(running.metadata["execution_snapshot"]["execution_dir"])
    listing_before = _generation_listing(generation)

    assert run_child() == worker_job.ADMISSION_DEFERRED_EXIT_CODE

    [deferred] = list_queue(queue_root)
    assert deferred.status == QueueStatus.PENDING
    assert "RAM headroom" in queue_entry_admission_deferral_reason(deferred)
    assert _generation_listing(generation) == listing_before
    assert not orca_execution_started_evidence(rxn, deferred.metadata["execution_snapshot"])
    assert not state_path(rxn).exists()
    assert notifications == []
    assert list(shm.glob("orca_auto/attempt-*")) == []

    # Memory came back. The same generation is claimed again and runs once;
    # no recovery rebind was spent on a job that had never started.
    available["bytes"] = 2**63
    import orca_auto.core.queue.store as store_mod

    monkeypatch.setattr(store_mod, "queue_entry_admission_is_deferred", lambda _entry: False)
    reclaimed = dequeue_next(queue_root)
    assert reclaimed is not None
    for slot in list_slots(admission_root):
        from orca_auto.core.admission import release_slot

        release_slot(admission_root, slot.token)

    assert run_child() == 0

    [finished] = list_queue(queue_root)
    assert worker_job.RECOVERY_REBIND_COUNT_METADATA_KEY not in finished.metadata
    assert finished.metadata["execution_snapshot"] == running.metadata["execution_snapshot"]
    assert len(notifications) == 2


# --- what an operator sees ------------------------------------------------------------------


def test_queue_list_shows_why_an_orca_row_waits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Through the real command: ORCA rows are built by their own activity
    # source, so checking a record builder in isolation proves nothing here.
    import json

    from orca_auto.cli import main
    from orca_auto.orca.queue.adapter import requeue_running_entry

    runs_root = tmp_path / "orca_runs"
    job_dir = runs_root / "benzene"
    job_dir.mkdir(parents=True)
    (job_dir / "benzene.inp").write_text(
        "! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
    )
    fake_orca = tmp_path / "fake-orca"
    fake_orca.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {
                "runs_root": str(runs_root),
                "scheduler": {"max_active_simulations": 2},
                "orca": {"runtime": {}, "paths": {"orca_executable": str(fake_orca)}},
            }
        ),
        encoding="utf-8",
    )
    assert main(["run-dir", str(job_dir), "--config", str(config)]) == 0
    claimed = dequeue_next(runs_root)
    assert claimed is not None
    assert requeue_running_entry(
        runs_root, claimed.queue_id, expected_entry=claimed, admission_deferral_reason=_REFUSAL
    )
    capsys.readouterr()

    def listed() -> tuple[dict[str, Any], str]:
        assert main(["queue", "list", "--json", "--config", str(config)]) == 0
        payload = json.loads(capsys.readouterr().out)
        [row] = [a for a in payload["activities"] if a["metadata"]["queue_id"] == claimed.queue_id]
        assert main(["queue", "list", "--config", str(config)]) == 0
        return row, capsys.readouterr().out

    row, text = listed()
    assert row["status"] == "pending"
    assert row["metadata"]["admission_deferral_reason"] == _REFUSAL
    assert "(waiting for resources)" in text

    # Claimed again: the row no longer waits, and says so.
    import orca_auto.core.queue.store as store_mod

    monkeypatch.setattr(store_mod, "queue_entry_admission_is_deferred", lambda _entry: False)
    assert dequeue_next(runs_root) is not None
    row, text = listed()
    assert row["metadata"]["admission_deferral_reason"] == ""
    assert "(waiting for resources)" not in text
