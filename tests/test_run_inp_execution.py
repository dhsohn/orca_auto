from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import pytest

from orca_auto.orca import execution as run_inp_execution
from orca_auto.orca.run_context import RunExecutionContext
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


def test_execute_locked_run_recovers_state_inside_the_run_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # State recovery runs after the reaction lock and before admission/execution.
    # Engine ownership has already been reconciled through the admission store.
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

    def fake_recover(_reaction_dir: Path, *, logger: logging.Logger) -> bool:
        del logger
        events.append("recover")
        return False

    def fake_run_with_state(**_kwargs: object) -> int:
        events.append("run")
        return 0

    monkeypatch.setattr(run_inp_execution, "acquire_run_lock", fake_run_lock)
    monkeypatch.setattr(run_inp_execution, "recover_crashed_state", fake_recover)
    monkeypatch.setattr(run_inp_execution, "_admission_context", fake_admission)
    monkeypatch.setattr(
        run_inp_execution,
        "load_or_create_state",
        lambda *_a, **_k: ({"status": "created"}, False),
    )
    monkeypatch.setattr(run_inp_execution, "save_state", lambda *_a, **_k: None)
    monkeypatch.setattr(run_inp_execution, "run_with_state", fake_run_with_state)
    context = cast(
        RunExecutionContext,
        SimpleNamespace(
            reaction_dir=tmp_path / "rxn",
            selected_inp=tmp_path / "rxn.inp",
            admission_root=None,
            reservation_token=None,
            admission_app_name=None,
            admission_task_id="",
            max_retries=2,
            cfg=None,
        ),
    )

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=True),
        context,
        runner_cls=object,
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
    monkeypatch: pytest.MonkeyPatch,
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

    def exit_with_result(
        _reaction_dir: Path,
        current_state: dict[str, object],
        _selected_inp: Path,
        **_kwargs: object,
    ) -> int:
        finalized_states.append(dict(current_state))
        return 0

    monkeypatch.setattr(run_inp_execution, "acquire_run_lock", fake_run_lock)
    monkeypatch.setattr(
        run_inp_execution,
        "recover_crashed_state",
        lambda _reaction_dir, *, logger: False,
    )
    monkeypatch.setattr(run_inp_execution, "_admission_context", fake_admission)
    monkeypatch.setattr(
        run_inp_execution,
        "existing_completed_out",
        lambda _selected_inp: {"out_path": reaction_dir / "rxn.out"},
    )
    monkeypatch.setattr(
        run_inp_execution,
        "load_or_create_state",
        lambda *_args, **_kwargs: (state, False),
    )
    monkeypatch.setattr(
        run_inp_execution,
        "save_state",
        lambda _reaction_dir, current_state: saved_states.append(dict(current_state)),
    )
    monkeypatch.setattr(run_inp_execution, "_exit_with_result", exit_with_result)
    context = cast(
        RunExecutionContext,
        SimpleNamespace(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            admission_root=tmp_path,
            reservation_token=None,
            admission_app_name=None,
            admission_task_id="queue-task-id",
            max_retries=2,
            cfg=None,
        ),
    )

    exit_code = run_inp_execution.execute_locked_run(
        SimpleNamespace(force=False),
        context,
        runner_cls=object,
    )

    assert exit_code == 0
    assert saved_states == [{"job_id": "queue-task-id"}]
    assert finalized_states == [{"job_id": "queue-task-id"}]
