from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import reserve_slot
from orca_auto.core.config import CommonResourceConfig
from orca_auto.core.engine_scratch import _workspace as workspace_mod
from orca_auto.orca import execution, output_adoption
from orca_auto.orca.config import AppConfig, PathsConfig, load_config
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.orca_runner import OrcaRunner
from orca_auto.orca.scratch_config import ScratchConfig
from orca_auto.orca.state_reading import load_state
from tests.conftest import bound_run_context, make_app_cfg, make_run_context, write_run_state


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

    def fake_recover(_reaction_dir: Path) -> bool:
        events.append("recover")
        return False

    def fake_run_attempt(*_args: object, **_kwargs: object) -> int:
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
    monkeypatch.setattr(execution, "run_attempt", fake_run_attempt)
    context = make_run_context(AppConfig(), tmp_path / "rxn", tmp_path / "rxn.inp")

    exit_code = execution.execute_locked_run(context, stop_requested=lambda: False)

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
        lambda _reaction_dir: False,
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

    exit_code = execution.execute_locked_run(context, stop_requested=lambda: False)

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
    rc = execute_orca_run(
        make_run_context(cfg, reaction, inp, admission_token=token), stop_requested=lambda: False
    )
    saved = load_state(reaction)

    assert rc == 1
    assert saved is not None
    assert saved["run_id"] == "run_crashed"  # Preserved run_id
    assert saved["status"] == "failed"
    final_result = saved["final_result"]
    assert final_result is not None
    assert final_result["resumed"]


def test_the_run_builds_its_one_runner_from_the_bound_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_orca: Path,
    fake_shm: Path,
) -> None:
    reaction = tmp_path / "rxn"
    reaction.mkdir()
    inp = reaction / "rxn.inp"
    inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    cfg = make_app_cfg(
        tmp_path,
        orca_executable=fake_orca,
        scratch=ScratchConfig(root=str(fake_shm / "orca_auto"), min_free_gb=2),
        resources=CommonResourceConfig(max_memory_gb_per_task=3),
    )
    bound = bound_run_context(cfg, inp, admission_token="slot-1")
    # The configuration changed after submission; the run keeps what was bound.
    context = replace(
        bound,
        cfg=replace(
            cfg,
            paths=PathsConfig(orca_executable="/changed/orca"),
            resources=CommonResourceConfig(max_memory_gb_per_task=99),
        ),
    )
    built: list[dict[str, Any]] = []

    class RecordingRunner(OrcaRunner):
        def __init__(self, orca_executable: str, **kwargs: Any) -> None:
            built.append({"orca_executable": orca_executable, **kwargs})
            super().__init__(orca_executable, **kwargs)

    def stop_before_state(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("stop before the first state write")

    @contextmanager
    def passthrough(*_args: object) -> Iterator[None]:
        yield

    monkeypatch.setattr(execution, "OrcaRunner", RecordingRunner)
    monkeypatch.setattr(execution, "_child_admission_slot", passthrough)
    monkeypatch.setattr(execution, "load_or_create_state", stop_before_state)
    monkeypatch.setattr(workspace_mod, "_linux_available_memory_bytes", lambda: 2**63)

    def stop_requested() -> bool:
        return False

    with pytest.raises(RuntimeError, match="stop before the first state write"):
        execution.execute_locked_run(context, stop_requested=stop_requested)

    [runner] = built
    snapshot = context.execution_snapshot
    scratch_policy = runner.pop("scratch_policy")
    assert (
        scratch_policy.root,
        scratch_policy.min_free_bytes,
        scratch_policy.max_task_memory_bytes,
    ) == (fake_shm / "orca_auto", 2 * 1024**3, context.resource_request["max_memory_gb"] * 1024**3)
    assert context.resource_request["max_memory_gb"] == 3
    assert callable(runner.pop("prepare_running_job"))
    assert callable(runner.pop("register_running_job"))
    assert runner == {
        "orca_executable": str(fake_orca.resolve()),
        "executable_identity": snapshot["executable_identities"]["orca"],
        "execution_dir": Path(snapshot["execution_dir"]),
        "execution_dir_identity": snapshot["execution_dir_identity"],
        "verify_snapshot": context.verify_snapshot,
        "stop_requested": stop_requested,
    }
