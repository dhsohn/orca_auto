from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from orca_auto.core.admission import reserve_slot
from orca_auto.orca import execution, output_adoption
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.orca_runner import OrcaRunner
from orca_auto.orca.state import save_state
from orca_auto.orca.state_reading import load_state
from tests.conftest import make_run_context, write_run_state


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
            "status": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "attempts": [],
            "final_result": None,
        },
    )


def test_run_with_state_rejects_admitted_runner_without_process_registrar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunnerWithoutRegistrar:
        def __init__(self, _orca_executable: str) -> None:
            pass

    monkeypatch.setattr(
        execution,
        "started_notification_callback",
        lambda _cfg: None,
    )
    monkeypatch.setattr(execution, "run_attempts", lambda *_args, **_kwargs: 0)
    context = make_run_context(
        AppConfig(), tmp_path / "rxn", tmp_path / "rxn.inp", admission_token="slot-1"
    )

    with pytest.raises(
        TypeError,
        match="Admitted ORCA runner does not support engine-process registration",
    ):
        execution.run_with_state(
            context,
            runner_cls=RunnerWithoutRegistrar,
            resumed=False,
            state={},
            runner=None,
        )


def test_recover_crashed_state_transitions_running_state_to_failed(
    tmp_path: Path,
) -> None:
    # Called under the run lock (exclusive owner), a running/retrying state is
    # a crash and is reconciled to failed/crashed_recovery.
    reaction_dir = tmp_path / "rxn"
    _write_running_state(reaction_dir)

    recovered = execution.recover_crashed_state(
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

    events: list[str] = []

    @contextmanager
    def fake_run_lock(_reaction_dir: Path):
        events.append("lock_enter")
        try:
            yield
        finally:
            events.append("lock_exit")

    @contextmanager
    def fake_admission(_context: object):
        events.append("admission_enter")
        try:
            yield
        finally:
            events.append("admission_exit")

    def fake_recover(_reaction_dir: Path, *, logger: logging.Logger) -> bool:
        del logger
        events.append("recover")
        return False

    def fake_run_with_state(*_args: object, **_kwargs: object) -> int:
        events.append("run")
        return 0

    monkeypatch.setattr(execution, "acquire_run_lock", fake_run_lock)
    monkeypatch.setattr(execution, "recover_crashed_state", fake_recover)
    monkeypatch.setattr(execution, "_child_admission_slot", fake_admission)
    monkeypatch.setattr(
        execution,
        "load_or_create_state",
        lambda *_a, **_k: ({"status": "created"}, False),
    )
    monkeypatch.setattr(execution, "save_state", lambda *_a, **_k: None)
    monkeypatch.setattr(execution, "run_with_state", fake_run_with_state)
    context = make_run_context(AppConfig(), tmp_path / "rxn", tmp_path / "rxn.inp")

    exit_code = execution.execute_locked_run(context, runner_cls=object)

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

    reaction_dir = tmp_path / "rxn"
    selected_inp = reaction_dir / "rxn.inp"
    state = {"job_id": "generated-job-id"}
    saved_states: list[dict[str, object]] = []
    finalized_states: list[dict[str, object]] = []

    @contextmanager
    def fake_run_lock(_reaction_dir: Path):
        yield

    @contextmanager
    def fake_admission(_context: object):
        yield

    def exit_with_result(
        _reaction_dir: Path,
        current_state: dict[str, object],
        _selected_inp: Path,
        **_kwargs: object,
    ) -> int:
        finalized_states.append(dict(current_state))
        return 0

    monkeypatch.setattr(execution, "acquire_run_lock", fake_run_lock)
    monkeypatch.setattr(
        execution,
        "recover_crashed_state",
        lambda _reaction_dir, *, logger: False,
    )
    monkeypatch.setattr(execution, "_child_admission_slot", fake_admission)
    monkeypatch.setattr(
        output_adoption,
        "existing_completed_out",
        lambda _selected_inp: {"out_path": reaction_dir / "rxn.out"},
    )
    monkeypatch.setattr(
        output_adoption,
        "load_or_create_state",
        lambda *_args, **_kwargs: (state, False),
    )
    monkeypatch.setattr(
        output_adoption,
        "save_state",
        lambda _reaction_dir, current_state: saved_states.append(dict(current_state)),
    )
    monkeypatch.setattr(output_adoption, "exit_with_result", exit_with_result)
    context = make_run_context(
        AppConfig(), reaction_dir, selected_inp, admission_task_id="queue-task-id"
    )

    exit_code = execution.execute_locked_run(context, runner_cls=object)

    assert exit_code == 0
    assert saved_states == [{"job_id": "queue-task-id"}]
    assert finalized_states == [{"job_id": "queue-task-id"}]


def test_crash_recovery_finalizes_recorded_failure_without_rerunning(
    tmp_path: Path,
    config_path: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "orca_runs"
    reaction = runs_root / "rxn_crash"
    reaction.mkdir(parents=True)
    inp = reaction / "rxn.inp"
    inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    config = config_path(runs_root=runs_root)

    # Simulate a crashed run: status=running with a recorded failed attempt
    # and no run.lock holder.
    write_run_state(
        reaction,
        status="running",
        run_id="run_crashed",
        selected_inp=inp,
        attempts=[
            {
                "index": 1,
                "inp_path": str(inp),
                "out_path": str(reaction / "rxn.out"),
                "return_code": 1,
                "analyzer_status": "error_scf",
                "analyzer_reason": "scf_not_converged",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
            }
        ],
    )

    def _no_rerun(_self: OrcaRunner, inp_path: Path) -> None:
        pytest.fail(f"crash recovery must not rerun ORCA on {inp_path}")

    monkeypatch.setattr(OrcaRunner, "run", _no_rerun)
    token = reserve_slot(
        runs_root / ".admission",
        1,
        work_dir=str(reaction),
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    cfg = load_config(str(config))
    rc = execute_orca_run(make_run_context(cfg, reaction, inp, admission_token=token))
    saved = load_state(reaction)

    assert rc == 1
    assert saved is not None
    assert saved["run_id"] == "run_crashed"  # Preserved run_id
    assert saved["status"] == "failed"
    final_result = saved["final_result"]
    assert final_result is not None
    assert final_result["resumed"]
