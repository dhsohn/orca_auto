from __future__ import annotations

import logging
from pathlib import Path

import pytest

from orca_auto.orca.commands import run_inp_execution
from orca_auto.orca.state import load_state, save_state


def _write_running_state(reaction_dir: Path) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    save_state(
        reaction_dir,
        {
            "run_id": "run_active",
            "reaction_dir": str(reaction_dir),
            "selected_inp": str(inp),
            "max_retries": 2,
            "status": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "attempts": [],
            "final_result": None,
        },
    )


def test_recover_crashed_state_transitions_running_state_to_failed(
    tmp_path: Path,
) -> None:
    # Called under the run lock (exclusive owner), a running/retrying state is
    # a crash and is reconciled to failed/crashed_recovery.
    reaction_dir = tmp_path / "rxn"
    _write_running_state(reaction_dir)

    recovered = run_inp_execution.recover_crashed_state(
        reaction_dir,
        logger=logging.getLogger("test_recover_crashed_state"),
    )

    assert recovered is True
    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "failed"
    assert state["final_result"] == {
        "status": "failed",
        "reason": "crashed_recovery",
        "analyzer_status": "incomplete",
    }


def _write_interrupted_state(reaction_dir: Path) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    save_state(
        reaction_dir,
        {
            "run_id": "run_interrupted",
            "reaction_dir": str(reaction_dir),
            "selected_inp": str(inp),
            "max_retries": 2,
            "status": "failed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "attempts": [{"index": 1, "out_path": str(reaction_dir / "rxn.out")}],
            "final_result": {"status": "failed", "reason": "interrupted_by_user"},
        },
    )


def test_recover_crashed_state_reaps_orphan_for_interrupted_failed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A local Ctrl-C leaves status=failed/interrupted_by_user (which
    # load_or_create_state still resumes). The orphaned ORCA group must be
    # reaped even though this is not a running/retrying "crash", or the resumed
    # rerun races a live calculation over the same output.
    reaction_dir = tmp_path / "rxn"
    _write_interrupted_state(reaction_dir)
    reaped: list[Path] = []

    def _fake_reaper(reaction_dir: Path, *, logger: logging.Logger) -> bool:
        reaped.append(reaction_dir)
        return False

    monkeypatch.setattr(run_inp_execution, "recover_orphaned_orca_process", _fake_reaper)

    recovered = run_inp_execution.recover_crashed_state(
        reaction_dir,
        logger=logging.getLogger("test_recover_crashed_state"),
    )

    # The status transition itself is a no-op (not running/retrying)...
    assert recovered is False
    state = load_state(reaction_dir)
    assert state is not None and state["status"] == "failed"
    # ...but the orphan reaper ran regardless.
    assert reaped == [reaction_dir]


def test_execute_locked_run_recovers_inside_the_run_lock(
    tmp_path: Path,
) -> None:
    # The crash/orphan recovery must run AFTER the run lock is held and BEFORE
    # the run executes, so a concurrent invocation's freshly started ORCA can
    # never be mistaken for an orphan and killed.
    from contextlib import contextmanager
    from types import SimpleNamespace

    events: list[str] = []

    @contextmanager
    def fake_run_lock(_reaction_dir: Path):
        events.append("lock_enter")
        try:
            yield
        finally:
            events.append("lock_exit")

    @contextmanager
    def fake_admission(**_kwargs: object):
        events.append("admission_enter")
        try:
            yield
        finally:
            events.append("admission_exit")

    def fake_recover(_reaction_dir: Path) -> bool:
        events.append("recover")
        return False

    def fake_run_with_state(**_kwargs: object) -> int:
        events.append("run")
        return 0

    execution = SimpleNamespace(
        acquire_run_lock=fake_run_lock,
        _recover_crashed_state=fake_recover,
        _admission_context=fake_admission,
        load_or_create_state=lambda *_a, **_k: ({"status": "created"}, False),
        _to_resolved_local=lambda value: value,
        save_state=lambda *_a, **_k: None,
        _run_with_state=fake_run_with_state,
    )
    deps = SimpleNamespace(execution=execution)
    context = SimpleNamespace(
        reaction_dir=tmp_path / "rxn",
        selected_inp=tmp_path / "rxn.inp",
        admission_root=None,
        reservation_token=None,
        admission_app_name=None,
        admission_task_id="",
        max_retries=2,
        cfg=None,
    )

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=True),
        context,
        runner_cls=object,
        deps=deps,
    )

    assert exit_code == 0
    assert events == [
        "lock_enter",
        "recover",
        "admission_enter",
        "run",
        "admission_exit",
        "lock_exit",
    ]


def test_existing_completed_exit_stamps_queue_task_id_before_terminal_artifacts(
    tmp_path: Path,
) -> None:
    """Existing-output replay must retain the queue generation's task ID."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    reaction_dir = tmp_path / "rxn"
    selected_inp = reaction_dir / "rxn.inp"
    state = {"job_id": "generated-job-id"}
    saved_states: list[dict[str, object]] = []
    finalized_states: list[dict[str, object]] = []

    @contextmanager
    def fake_run_lock(_reaction_dir: Path):
        yield

    @contextmanager
    def fake_admission(**_kwargs: object):
        yield

    def existing_completed_exit(
        *,
        reaction_dir: Path,
        selected_inp: Path,
        admission_root: Path,
        reservation_token: str | None,
        max_retries: int,
        admission_task_id: str | None,
    ) -> int | None:
        return run_inp_execution.existing_completed_exit(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            admission_root=admission_root,
            reservation_token=reservation_token,
            max_retries=max_retries,
            admission_task_id=admission_task_id,
            deps=SimpleNamespace(
                execution=execution,
                statuses=SimpleNamespace(
                    RunStatus=SimpleNamespace(COMPLETED="completed"),
                    AnalyzerStatus=SimpleNamespace(COMPLETED="completed"),
                ),
            ),
        )

    def exit_with_result(
        _reaction_dir: Path,
        current_state: dict[str, object],
        _selected_inp: Path,
        **_kwargs: object,
    ) -> int:
        finalized_states.append(dict(current_state))
        return 0

    execution = SimpleNamespace(
        acquire_run_lock=fake_run_lock,
        _recover_crashed_state=lambda _reaction_dir: False,
        _admission_context=fake_admission,
        _existing_completed_exit=existing_completed_exit,
        _existing_completed_out=lambda _selected_inp: {"out_path": reaction_dir / "rxn.out"},
        load_or_create_state=lambda *_args, **_kwargs: (state, False),
        save_state=lambda _reaction_dir, current_state: saved_states.append(dict(current_state)),
        _exit_with_result=exit_with_result,
        _emit=lambda *_args, **_kwargs: None,
        _to_resolved_local=lambda value: value,
    )
    context = SimpleNamespace(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        admission_root=tmp_path,
        reservation_token=None,
        admission_app_name=None,
        admission_task_id="queue-task-id",
        max_retries=2,
        cfg=None,
    )

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=False),
        context,
        runner_cls=object,
        deps=SimpleNamespace(execution=execution),
    )

    assert exit_code == 0
    assert saved_states == [{"job_id": "queue-task-id"}]
    assert finalized_states == [{"job_id": "queue-task-id"}]


def test_execute_locked_run_completes_recovery_prepare_after_safe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-process recovery failure must not strand a managed slot pending."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    events: list[str] = []

    @contextmanager
    def fake_run_lock(_reaction_dir: Path):
        events.append("lock_enter")
        try:
            yield
        finally:
            events.append("lock_exit")

    def fail_recovery(_reaction_dir: Path) -> None:
        events.append("recover")
        raise ValueError("corrupt job state")

    def complete(*_args: object) -> SimpleNamespace:
        events.append("complete")
        return SimpleNamespace()

    monkeypatch.setattr(
        run_inp_execution,
        "get_slot",
        lambda *_args: SimpleNamespace(engine_process_state="idle"),
    )
    monkeypatch.setattr(
        run_inp_execution,
        "build_slot_engine_process_preparer",
        lambda *_args: lambda: events.append("prepare"),
    )
    monkeypatch.setattr(
        run_inp_execution,
        "complete_slot_engine_process",
        complete,
    )
    execution = SimpleNamespace(
        acquire_run_lock=fake_run_lock,
        _recover_crashed_state=fail_recovery,
    )
    context = SimpleNamespace(
        reaction_dir=tmp_path / "rxn",
        selected_inp=tmp_path / "rxn.inp",
        admission_root=tmp_path,
        reservation_token="slot",
        admission_app_name=None,
        admission_task_id="",
        max_retries=2,
        cfg=None,
    )

    with pytest.raises(ValueError, match="corrupt job state"):
        run_inp_execution.execute_locked_run(
            SimpleNamespace(force=True),
            context,
            runner_cls=object,
            deps=SimpleNamespace(execution=execution),
        )

    assert events == ["lock_enter", "prepare", "recover", "complete", "lock_exit"]


def test_execute_locked_run_keeps_recovery_prepare_for_process_recovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous process recovery retains the pending fence."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    events: list[str] = []

    @contextmanager
    def fake_run_lock(_reaction_dir: Path):
        events.append("lock_enter")
        try:
            yield
        finally:
            events.append("lock_exit")

    def fail_recovery(_reaction_dir: Path) -> None:
        events.append("recover")
        raise run_inp_execution.OrcaProcessRecoveryError("ambiguous process")

    def complete(*_args: object) -> SimpleNamespace:
        events.append("complete")
        return SimpleNamespace()

    monkeypatch.setattr(
        run_inp_execution,
        "get_slot",
        lambda *_args: SimpleNamespace(engine_process_state="idle"),
    )
    monkeypatch.setattr(
        run_inp_execution,
        "build_slot_engine_process_preparer",
        lambda *_args: lambda: events.append("prepare"),
    )
    monkeypatch.setattr(
        run_inp_execution,
        "complete_slot_engine_process",
        complete,
    )
    execution = SimpleNamespace(
        acquire_run_lock=fake_run_lock,
        _recover_crashed_state=fail_recovery,
    )
    context = SimpleNamespace(
        reaction_dir=tmp_path / "rxn",
        selected_inp=tmp_path / "rxn.inp",
        admission_root=tmp_path,
        reservation_token="slot",
        admission_app_name=None,
        admission_task_id="",
        max_retries=2,
        cfg=None,
    )

    with pytest.raises(run_inp_execution.OrcaProcessRecoveryError, match="ambiguous process"):
        run_inp_execution.execute_locked_run(
            SimpleNamespace(force=True),
            context,
            runner_cls=object,
            deps=SimpleNamespace(execution=execution),
        )

    assert events == ["lock_enter", "prepare", "recover", "lock_exit"]
