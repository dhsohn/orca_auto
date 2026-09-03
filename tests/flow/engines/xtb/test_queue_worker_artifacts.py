from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.queue import processes as queue_processes_mod
from orca_auto.flow.engines.xtb import execution as worker_exec
from orca_auto.flow.engines.xtb import queue_runtime as queue_cmd
from orca_auto.flow.engines.xtb import state as state_mod
from orca_auto.flow.engines.xtb import terminal as terminal_mod
from orca_auto.flow.engines.xtb import worker_terminal as worker_terminal_mod
from orca_auto.flow.engines.xtb.execution import WorkerExecutionOutcome
from tests.flow.engines.xtb.factories import (
    fake_reserve_slot as _fake_reserve_slot,
)
from tests.flow.engines.xtb.factories import (
    make_cfg as _make_cfg,
)
from tests.flow.engines.xtb.factories import (
    make_entry as _make_entry,
)
from tests.flow.engines.xtb.factories import (
    make_result as _make_result,
)


def test_write_execution_artifacts_skips_without_job_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = SimpleNamespace(task_id="job-1", queue_id="queue-1", metadata={})
    result = _make_result(tmp_path / "selected.xyz", status="completed", reason="completed")

    monkeypatch.setattr(
        worker_terminal_mod,
        "write_state",
        lambda *args, **kwargs: pytest.fail("write_state should not run"),
    )

    queue_cmd._write_execution_artifacts(entry, result)


def test_write_execution_artifacts_includes_ranking_summary_in_state(tmp_path: Path) -> None:
    job_dir = tmp_path / "ranking-job"
    job_dir.mkdir()
    selected_xyz = job_dir / "candidate.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = SimpleNamespace(
        task_id="job-1",
        queue_id="queue-1",
        app_name="orca_auto",
        task_kind="xtb_ranking",
        engine="xtb",
        priority=10,
        enqueued_at="2026-04-19T23:59:00Z",
        metadata={"job_dir": str(job_dir)},
    )
    result = queue_cmd.XtbRunResult(
        status="completed",
        reason="completed",
        command=("xtb", str(selected_xyz)),
        exit_code=0,
        started_at="2026-04-20T00:00:00Z",
        finished_at="2026-04-20T00:05:00Z",
        stdout_log=str((job_dir / "xtb.stdout.log").resolve()),
        stderr_log=str((job_dir / "xtb.stderr.log").resolve()),
        selected_input_xyz=str(selected_xyz.resolve()),
        job_type="ranking",
        reaction_key="rxn-1",
        input_summary={"candidate_paths": [str(selected_xyz.resolve())]},
        candidate_count=1,
        selected_candidate_paths=(str(selected_xyz.resolve()),),
        candidate_details=({"path": str(selected_xyz.resolve())},),
        analysis_summary={
            "candidate_paths": [str(selected_xyz.resolve())],
            "best_candidate_path": str(selected_xyz.resolve()),
            "best_total_energy": -12.34,
        },
        manifest_path=str((job_dir / "xtb_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
    )

    queue_cmd._write_execution_artifacts(entry, result)

    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["engine_payload"]["analysis_summary"] == {
        "candidate_paths": [str(selected_xyz.resolve())],
        "best_candidate_path": str(selected_xyz.resolve()),
        "best_total_energy": -12.34,
    }
    assert not (job_dir / "job_report.json").exists()


def test_write_running_state_skips_without_job_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    entry = SimpleNamespace(task_id="job-1", metadata={})
    monkeypatch.setattr(
        worker_terminal_mod,
        "write_state",
        lambda *args, **kwargs: pytest.fail("write_state should not run"),
    )
    queue_cmd._write_running_state(cfg, entry)


def test_write_running_state_records_worker_job_pid(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    job_dir = Path(cfg.runtime.allowed_root) / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)

    queue_cmd._write_running_state(cfg, entry, worker_job_pid=4242)

    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["process"]["worker_pid"] == 4242


def test_terminate_process_returns_immediately_for_finished_process() -> None:
    proc = SimpleNamespace(poll=lambda: 0)
    queue_cmd._terminate_process(cast(Any, proc))


def test_terminate_process_uses_fallback_terminate_and_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killpg_calls: list[tuple[int, int]] = []

    class _Process:
        pid = 4321

        def __init__(self) -> None:
            self.terminate_called = False
            self.kill_called = False
            self.wait_calls = 0

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True

        def wait(self, timeout: int) -> None:
            self.wait_calls += 1
            raise subprocess.TimeoutExpired(cmd="xtb", timeout=timeout)

    def fake_killpg(pid: int, sig: int) -> None:
        killpg_calls.append((pid, sig))
        if len(killpg_calls) == 1:
            raise ProcessLookupError("missing")
        raise PermissionError("denied")

    monkeypatch.setattr(queue_processes_mod.os, "killpg", fake_killpg)

    proc = _Process()
    queue_cmd._terminate_process(cast(Any, proc))

    assert killpg_calls == [
        (4321, signal.SIGTERM),
        (4321, signal.SIGKILL),
    ]
    assert proc.terminate_called is True
    assert proc.kill_called is True
    assert proc.wait_calls == 2


def test_terminate_process_swallows_terminate_and_kill_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        pid = 5555

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            raise RuntimeError("terminate failed")

        def kill(self) -> None:
            raise RuntimeError("kill failed")

        def wait(self, timeout: int) -> None:
            raise subprocess.TimeoutExpired(cmd="xtb", timeout=timeout)

    monkeypatch.setattr(
        queue_processes_mod.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError("missing")),
    )

    queue_cmd._terminate_process(cast(Any, _Process()))


def test_try_reserve_admission_slot_uses_resolved_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.runtime.resolved_admission_root = str(tmp_path / "resolved-admission")
    cfg.runtime.resolved_admission_limit = 5
    calls: list[tuple[str, int, str, str]] = []

    monkeypatch.setattr(
        queue_cmd,
        "reserve_slot",
        lambda root, limit, *, source, app_name, **_kwargs: _fake_reserve_slot(
            calls, root, limit, source, app_name
        ),
    )

    assert queue_cmd._try_reserve_admission_slot(cfg) == "slot-1"
    assert calls == [
        (
            str(tmp_path / "resolved-admission"),
            5,
            "orca_auto.flow.engines.xtb.queue_worker",
            "orca_auto_xtb",
        )
    ]


def test_run_worker_job_processes_loaded_entry_and_releases_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)
    released: list[tuple[str, str]] = []
    processed: list[dict[str, Any]] = []

    monkeypatch.setattr(worker_exec, "load_config", lambda _path=None: cfg)
    monkeypatch.setattr(worker_exec, "list_queue", lambda _root: [entry])
    monkeypatch.setattr(
        worker_exec, "release_slot", lambda root, token: released.append((root, token))
    )
    monkeypatch.setattr(worker_exec, "install_shutdown_signal_handlers", lambda _callback: None)

    def fake_process_dequeued_entry(*args: object, **kwargs: object) -> WorkerExecutionOutcome:
        processed.append({"args": args, "kwargs": kwargs})
        return WorkerExecutionOutcome(
            result=_make_result(selected_xyz, status="completed", reason="completed")
        )

    monkeypatch.setattr(
        worker_exec,
        "process_dequeued_entry",
        fake_process_dequeued_entry,
    )

    exit_code = worker_exec.run_worker_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=queue_root,
        queue_id=entry.queue_id,
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert exit_code == 0
    assert processed[0]["args"] == (cfg, entry)
    assert processed[0]["kwargs"]["queue_root"] == queue_root.resolve()
    assert released == []


def test_run_worker_job_uses_dependency_config_and_admission_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1")
    released: list[tuple[str, str]] = []
    deps = worker_exec.build_worker_execution_dependencies(
        config=worker_exec.WorkerConfigDependencies(
            load_config=lambda path: cfg,
            queue_entry_by_id=lambda root, queue_id: entry,
        ),
        admission=worker_exec.WorkerAdmissionDependencies(
            activate_reserved_slot=lambda *args, **kwargs: object(),
            release_slot=lambda root, token: released.append((str(root), token)),
        ),
    )
    captured: dict[str, Any] = {}

    def fake_run_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        assert kwargs["load_config_fn"]("/tmp/orca_auto.yaml") is cfg
        assert kwargs["find_queue_entry_fn"](tmp_path / "queue", "queue-1") is entry
        assert kwargs["dependencies_fn"]() is deps
        return 0

    monkeypatch.setattr(
        worker_exec,
        "run_engine_worker_child_job",
        fake_run_worker_child_job,
    )

    rc = worker_exec.run_worker_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        dependencies=deps,
    )

    assert rc == 0
    assert captured["queue_id"] == "queue-1"


def test_terminal_summary_helpers_cover_status_reason_and_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, job_type="path_search")

    summary = terminal_mod.load_terminal_summary(
        tmp_path,
        entry,
        rc=None,
        job_dir_fn=lambda _entry: job_dir,
        load_state_fn=lambda _job_dir: {
            "schema_version": 1,
            "engine": "xtb",
            "status": {},
            "engine_payload": {"candidate_count": "bad"},
        },
        queue_entry_by_id_fn=lambda _root, _queue_id: SimpleNamespace(
            status=SimpleNamespace(value="cancelled"),
            error="",
        ),
    )

    assert summary == terminal_mod.TerminalSummary(
        queue_id="queue-1",
        job_id="job-1",
        status="cancelled",
        reason="cancel_requested",
        metadata_update={},
    )
    assert terminal_mod.terminal_status({}, None, 0) == "completed"
    assert terminal_mod.terminal_status({}, None, 1) == "failed"
    assert (
        terminal_mod.terminal_status({"schema_version": 1, "status": {"state": "running"}}, None, 1)
        == "failed"
    )
    assert terminal_mod.terminal_reason({}, None, status="completed", rc=None) == "completed"
    assert terminal_mod.terminal_reason({}, None, status="failed", rc=17) == "worker_exit_code_17"

    terminal_mod.print_terminal_summary(summary)
    output = capsys.readouterr().out
    assert "status: cancelled" in output


def test_terminal_summary_requires_explicit_dependencies(tmp_path: Path) -> None:
    load_terminal_summary = cast(Any, terminal_mod.load_terminal_summary)
    with pytest.raises(TypeError, match="job_dir_fn"):
        load_terminal_summary(
            tmp_path,
            SimpleNamespace(queue_id="queue-1", task_id="job-1", metadata={}),
        )


def test_ensure_terminal_queue_status_leaves_requeued_pending_row_alone(
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1", task_id="job-1")
    summary = terminal_mod.TerminalSummary(
        queue_id="queue-1",
        job_id="job-1",
        status="completed",
        reason="completed",
        metadata_update={"candidate_count": 0},
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a requeued pending row must not be marked terminal")

    terminal = terminal_mod.ensure_terminal_queue_status(
        tmp_path,
        entry,
        summary,
        queue_entry_by_id_fn=lambda _root, _queue_id: SimpleNamespace(
            status=SimpleNamespace(value="pending")
        ),
        mark_completed_fn=refuse,
        mark_cancelled_fn=refuse,
        mark_failed_fn=refuse,
    )

    assert terminal is False


def test_ensure_terminal_queue_status_reports_refused_mark_as_not_terminal(
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1", task_id="job-1")
    summary = terminal_mod.TerminalSummary(
        queue_id="queue-1",
        job_id="job-1",
        status="failed",
        reason="worker_exit_code_1",
    )

    terminal = terminal_mod.ensure_terminal_queue_status(
        tmp_path,
        entry,
        summary,
        queue_entry_by_id_fn=lambda _root, _queue_id: SimpleNamespace(
            status=SimpleNamespace(value="running")
        ),
        mark_completed_fn=lambda *_args, **_kwargs: pytest.fail("should mark failed"),
        mark_cancelled_fn=lambda *_args, **_kwargs: pytest.fail("should mark failed"),
        mark_failed_fn=lambda *_args, **_kwargs: None,
    )

    assert terminal is False


def test_ensure_terminal_queue_status_skips_terminal_and_marks_nonterminal(
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    summary = terminal_mod.TerminalSummary(
        queue_id="queue-1",
        job_id="job-1",
        status="failed",
        reason="worker_exit_code_1",
        metadata_update={"job_type": "ranking"},
    )
    calls: list[tuple[str, str, str, dict[str, object] | None]] = []

    terminal_mod.ensure_terminal_queue_status(
        tmp_path,
        entry,
        summary,
        queue_entry_by_id_fn=lambda _root, _queue_id: SimpleNamespace(
            status=SimpleNamespace(value="completed")
        ),
        mark_completed_fn=lambda *_args, **_kwargs: pytest.fail("terminal entry should be skipped"),
        mark_cancelled_fn=lambda *_args, **_kwargs: pytest.fail("terminal entry should be skipped"),
        mark_failed_fn=lambda *_args, **_kwargs: pytest.fail("terminal entry should be skipped"),
    )

    terminal_mod.ensure_terminal_queue_status(
        tmp_path,
        entry,
        summary,
        queue_entry_by_id_fn=lambda _root, _queue_id: SimpleNamespace(
            status=SimpleNamespace(value="running")
        ),
        mark_completed_fn=lambda *_args, **_kwargs: pytest.fail("should mark failed"),
        mark_cancelled_fn=lambda *_args, **_kwargs: pytest.fail("should mark failed"),
        mark_failed_fn=lambda root, queue_id, *, error, metadata_update, **_kwargs: calls.append(
            (root, queue_id, error, metadata_update)
        ),
    )

    assert calls == [
        (
            str(tmp_path),
            "queue-1",
            "worker_exit_code_1",
            {"job_type": "ranking"},
        )
    ]
