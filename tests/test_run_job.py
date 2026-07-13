from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca import worker_execution as worker_job
from orca_auto.orca.execution_binding import build_orca_execution_snapshot
from orca_auto.orca.orca_runner import OrcaRunner, WorkerShutdownInterrupt
from orca_auto.orca.queue.adapter import dequeue_next, enqueue, list_queue
from orca_auto.orca.state import load_state, new_state, save_state
from orca_auto.orca.worker_execution import execute_run_job


def _bound_orca_metadata(
    tmp_path: Path,
    reaction_dir: Path,
    *,
    force: bool = True,
) -> dict[str, Any]:
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
        max_retries=0,
        orca_executable=executable,
    )
    return {
        "reaction_dir": str(reaction_dir),
        "force": force,
        "source_selected_inp": str(selected),
        "selected_inp": snapshot["selected_inp"],
        "selected_input_xyz": "",
        "resource_request": resources,
        "max_retries": 0,
        "execution_snapshot": snapshot,
    }


@patch("orca_auto.orca.worker_execution._cmd_run_inp_execute", return_value=7)
def test_execute_run_job_builds_run_inp_execution_request(
    mock_execute: MagicMock,
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    selected_inp = reaction_dir / ".orca_auto_orca_executions" / "generation-1" / "rxn.inp"
    rc = execute_run_job(
        "/tmp/config.yaml",
        str(reaction_dir),
        selected_inp=selected_inp,
        force=True,
        reservation_token="slot_123",
        admission_app_name="orca_auto_orca",
        admission_task_id="task_123",
    )

    assert rc == 7
    args = mock_execute.call_args.args[0]
    assert args.config == "/tmp/config.yaml"
    assert args.reaction_dir == str(reaction_dir)
    assert args.force is True
    assert mock_execute.call_args.kwargs["reservation_token"] == "slot_123"
    assert mock_execute.call_args.kwargs["admission_app_name"] == "orca_auto_orca"
    assert mock_execute.call_args.kwargs["admission_task_id"] == "task_123"
    assert mock_execute.call_args.kwargs["reaction_dir"] == reaction_dir.resolve()
    assert mock_execute.call_args.kwargs["selected_inp"] == selected_inp.resolve()
    assert mock_execute.call_args.kwargs["cfg"] is None


def test_build_worker_child_command_uses_queue_identity(tmp_path: Path) -> None:
    command = worker_job.build_worker_child_command(
        config_path="/tmp/config.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
    )

    assert command[:3] == [sys.executable, "-m", worker_job.WORKER_JOB_MODULE]
    assert command[3:5] == ["--engine", "orca"]
    assert "--queue-root" in command
    assert str(tmp_path / "queue") in command
    assert "--queue-id" in command
    assert "queue-1" in command
    assert "--admission-token" in command
    assert "slot-1" in command
    assert "--admission-root" not in command


def test_run_worker_child_job_loads_queue_entry_and_preserves_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    reaction_dir = queue_root / "rxn"
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(queue_root),
            admission_root=str(tmp_path / "admission"),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    entry = QueueEntry(
        queue_id="queue-1",
        app_name="orca_auto_orca",
        task_id="task-1",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.RUNNING,
        metadata=_bound_orca_metadata(tmp_path, reaction_dir),
    )
    calls: dict[str, Any] = {}
    released: list[tuple[str, str]] = []

    monkeypatch.setattr(worker_job, "load_config", lambda _path: cfg)
    monkeypatch.setattr(worker_job, "_queue_entry_by_id", lambda _root, _queue_id: entry)
    monkeypatch.setattr(worker_job, "install_shutdown_signal_handlers", lambda _callback: None)
    monkeypatch.setattr(
        worker_job,
        "release_slot",
        lambda root, token: released.append((str(root), token)),
    )

    def fake_execute_run_job(*args: Any, **kwargs: Any) -> int:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return 5

    monkeypatch.setattr(worker_job, "execute_run_job", fake_execute_run_job)

    rc = worker_job.run_worker_child_job(
        config_path="/tmp/config.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 5
    assert calls["args"] == ("/tmp/config.yaml", str(reaction_dir))
    runner_cls = calls["kwargs"].pop("runner_cls")
    bound_cfg = calls["kwargs"].pop("cfg")
    assert issubclass(runner_cls, OrcaRunner)
    assert bound_cfg.runtime.default_max_retries == 0
    runner = runner_cls("/changed/orca")
    assert runner.orca_executable == str((tmp_path / "fake-orca").resolve())
    assert (
        runner._bound_executable_identity
        == entry.metadata["execution_snapshot"]["executable_identities"]["orca"]
    )
    assert calls["kwargs"] == {
        "force": True,
        "reservation_token": "slot-1",
        "admission_app_name": "orca_auto_orca",
        "admission_task_id": "task-1",
        "selected_inp": entry.metadata["selected_inp"],
    }
    state = new_state(reaction_dir, Path(entry.metadata["selected_inp"]), max_retries=0)
    save_state(reaction_dir, state)
    with patch.object(
        OrcaRunner,
        "run",
        return_value=SimpleNamespace(out_path="job.out", return_code=0),
    ):
        run_result = runner.run(Path(entry.metadata["selected_inp"]))
    saved = load_state(reaction_dir)
    assert saved is not None
    assert (
        run_result.execution_provenance["bound_selected_identity"]
        == entry.metadata["execution_snapshot"]["bound_selected_identity"]
    )
    assert released == []


def test_orca_worker_rejects_snapshotless_persisted_generation(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    reaction_dir = queue_root / "legacy-rxn"
    reaction_dir.mkdir(parents=True)
    entry = QueueEntry(
        queue_id="queue-legacy",
        app_name="orca_auto_orca",
        task_id="task-legacy",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.RUNNING,
        metadata={
            "reaction_dir": str(reaction_dir),
            "selected_inp": str(reaction_dir / "job.inp"),
            "resource_request": {"max_cores": 1, "max_memory_gb": 1},
        },
    )
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(queue_root)))

    with pytest.raises(ValueError, match="drain or resubmit"):
        worker_job._build_execution_context(
            cfg,
            entry,
            worker_config_path="/tmp/config.yaml",
            admission_token="slot-legacy",
        )


def test_process_dequeued_entry_returns_orca_worker_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    reaction_dir = queue_root / "rxn"
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(queue_root)))
    entry = QueueEntry(
        queue_id="queue-1",
        app_name="orca_auto_orca",
        task_id="task-1",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.RUNNING,
        metadata=_bound_orca_metadata(tmp_path, reaction_dir),
    )
    calls: dict[str, Any] = {}

    def fake_execute_run_job(*args: Any, **kwargs: Any) -> int:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return 4

    monkeypatch.setattr(worker_job, "execute_run_job", fake_execute_run_job)

    outcome = worker_job.process_dequeued_entry(
        cfg,
        entry,
        queue_root=queue_root,
        worker_config_path="/tmp/config.yaml",
        admission_token="slot-1",
        shutdown_requested=lambda: False,
    )

    assert outcome.exit_code == 4
    assert outcome.reaction_dir == str(reaction_dir)
    assert outcome.entry is entry
    assert calls["args"] == ("/tmp/config.yaml", str(reaction_dir))
    runner_cls = calls["kwargs"].pop("runner_cls")
    bound_cfg = calls["kwargs"].pop("cfg")
    assert issubclass(runner_cls, OrcaRunner)
    assert bound_cfg.runtime.default_max_retries == 0
    assert calls["kwargs"] == {
        "force": True,
        "reservation_token": "slot-1",
        "admission_app_name": "orca_auto_orca",
        "admission_task_id": "task-1",
        "selected_inp": entry.metadata["selected_inp"],
    }


def test_run_worker_child_job_finds_real_queue_entry_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    admission_root = tmp_path / "admission"
    rxn = queue_root / "rxn"
    rxn.mkdir(parents=True)
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-real",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    running = dequeue_next(queue_root)
    assert running is not None
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(queue_root),
            admission_root=str(admission_root),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    calls: dict[str, Any] = {}
    released: list[tuple[str, str]] = []

    monkeypatch.setattr(worker_job, "load_config", lambda _path: cfg)
    monkeypatch.setattr(worker_job, "install_shutdown_signal_handlers", lambda _callback: None)
    monkeypatch.setattr(
        worker_job,
        "release_slot",
        lambda root, token: released.append((str(root), token)),
    )

    def fake_execute_run_job(*args: Any, **kwargs: Any) -> int:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return 8

    monkeypatch.setattr(worker_job, "execute_run_job", fake_execute_run_job)

    rc = worker_job.run_worker_child_job(
        config_path="/tmp/config.yaml",
        queue_root=queue_root,
        queue_id=entry.queue_id,
        admission_token="slot-real",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 8
    assert calls["args"] == ("/tmp/config.yaml", str(rxn))
    assert calls["kwargs"]["force"] is True
    assert calls["kwargs"]["reservation_token"] == "slot-real"
    assert calls["kwargs"]["admission_app_name"] == "orca_auto_orca"
    assert calls["kwargs"]["admission_task_id"] == "task-real"
    assert calls["kwargs"]["selected_inp"] == entry.metadata["selected_inp"]
    assert released == []


def test_run_worker_child_job_requeues_on_worker_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    admission_root = tmp_path / "admission"
    rxn = queue_root / "rxn_shutdown"
    rxn.mkdir(parents=True)
    entry = enqueue(
        queue_root,
        str(rxn),
        force=True,
        task_id="task-shutdown",
        metadata=_bound_orca_metadata(tmp_path, rxn),
    )
    running = dequeue_next(queue_root)
    assert running is not None
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(queue_root),
            admission_root=str(admission_root),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    released: list[tuple[str, str]] = []

    monkeypatch.setattr(worker_job, "load_config", lambda _path: cfg)
    monkeypatch.setattr(worker_job, "install_shutdown_signal_handlers", lambda _callback: None)
    monkeypatch.setattr(
        worker_job,
        "release_slot",
        lambda root, token: released.append((str(root), token)),
    )
    monkeypatch.setattr(
        worker_job,
        "execute_run_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WorkerShutdownInterrupt),
    )

    rc = worker_job.run_worker_child_job(
        config_path="/tmp/config.yaml",
        queue_root=queue_root,
        queue_id=entry.queue_id,
        admission_token="slot-shutdown",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 0
    assert released == []
    [updated] = list_queue(queue_root)
    assert updated.queue_id == entry.queue_id
    assert updated.status == QueueStatus.PENDING
    assert updated.started_at == ""


def test_run_worker_child_job_releases_slot_when_entry_not_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    admission_root = tmp_path / "admission"
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(queue_root),
            admission_root=str(admission_root),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    released: list[tuple[str, str]] = []

    monkeypatch.setattr(worker_job, "load_config", lambda _path: cfg)
    monkeypatch.setattr(
        worker_job,
        "_queue_entry_by_id",
        lambda _root, _queue_id: QueueEntry(
            queue_id="queue-1",
            app_name="orca_auto_orca",
            task_id="task-1",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.PENDING,
            metadata={"reaction_dir": str(tmp_path / "rxn")},
        ),
    )
    monkeypatch.setattr(
        worker_job,
        "release_slot",
        lambda root, token: released.append((str(root), token)),
    )
    monkeypatch.setattr(
        worker_job,
        "execute_run_job",
        lambda *_args, **_kwargs: pytest.fail("entry should not execute"),
    )

    rc = worker_job.run_worker_child_job(
        config_path="/tmp/config.yaml",
        queue_root=queue_root,
        queue_id="queue-1",
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 1
    assert released == []


@patch("orca_auto.orca.worker_execution.run_worker_child_job", return_value=6)
def test_worker_job_main_returns_queue_child_status(mock_run_child: MagicMock) -> None:
    rc = worker_job.main(
        [
            "--config",
            "/tmp/config.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "queue-1",
            "--admission-token",
            "slot-1",
        ]
    )

    assert rc == 6
    mock_run_child.assert_called_once_with(
        config_path="/tmp/config.yaml",
        queue_root="/tmp/queue",
        queue_id="queue-1",
        admission_token="slot-1",
    )
