from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from orca_auto.core.admission import get_slot, release_slot, reserve_slot
from orca_auto.core.config.engines import load_xtb_md_config
from orca_auto.core.queue import (
    QueueStatus,
    dequeue_entry_if_pending,
    list_queue,
    mark_completed,
    request_cancel,
)
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.flow import activity
from orca_auto.xtb_md import execution
from orca_auto.xtb_md import submission as xtb_md_submission
from orca_auto.xtb_md.job_locations import list_job_records_for_cfg
from orca_auto.xtb_md.runner import run_xtb_md_attempt
from orca_auto.xtb_md.submission import APP_NAME, submit_job_dir


def _submit(case: Any, *, priority: int = 10) -> dict[str, Any]:
    return submit_job_dir(
        job_dir=str(case.job_dir),
        priority=priority,
        config_path=str(case.config_path),
    )


def _entry(case: Any, queue_id: str) -> Any:
    matches = [entry for entry in list_queue(case.runs_root) if entry.queue_id == queue_id]
    assert len(matches) == 1
    return matches[0]


def _reserve_managed_slot(case: Any, entry: Any) -> str:
    token = reserve_slot(
        case.admission_root,
        2,
        source="xtb-md-runtime-test",
        app_name=APP_NAME,
        task_id=entry.task_id,
        queue_id=entry.queue_id,
        work_dir=case.job_dir,
        engine_process_state="idle",
    )
    assert token is not None
    return token


def _run_claimed_entry(
    case: Any,
    entry: Any,
    admission_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    monkeypatch.setattr(execution, "install_shutdown_signal_handlers", lambda _callback: None)
    return execution.run_worker_job(
        config_path=str(case.config_path),
        queue_root=str(case.runs_root),
        queue_id=entry.queue_id,
        admission_token=admission_token,
    )


def test_submission_pins_exact_stable_xtb_version_and_cleans_rejected_generation(
    runtime_case_factory,
) -> None:
    accepted_case = runtime_case_factory(version="6.7.1")
    accepted = _submit(accepted_case)

    assert accepted["status"] == "queued"
    accepted_entry = _entry(accepted_case, accepted["queue_id"])
    assert accepted_entry.metadata["execution_snapshot"]["xtb_version"] == {
        "version": "6.7.1",
        "release_tag": "v6.7.1",
        "archive_sha256": "62a8d18778286e815292ee53d76ce447daf460a4dea3782c0f25cbac7019b5df",
    }

    rejected_case = runtime_case_factory(version="6.7.2")
    rejected = _submit(rejected_case)

    assert rejected["status"] == "failed"
    assert rejected["reason"] == "submission_failed"
    assert "supports stable xTB version 6.7.1" in rejected["stderr"]
    assert "reports 6.7.2" in rejected["stderr"]
    assert list_queue(rejected_case.runs_root) == []
    snapshot_root = rejected_case.job_dir / ".orca_auto_input_snapshots"
    assert not snapshot_root.exists() or list(snapshot_root.iterdir()) == []


def test_active_job_directory_duplicate_is_rejected_but_terminal_allows_new_generation(
    runtime_case_factory,
) -> None:
    case = runtime_case_factory()
    first = _submit(case, priority=3)
    duplicate = _submit(case, priority=4)

    assert first["status"] == "queued"
    assert duplicate["status"] == "failed"
    assert duplicate["reason"] == "submission_failed"
    assert "active xTB-MD generation already owns this job directory" in duplicate["stderr"]
    entries = list_queue(case.runs_root)
    assert len(entries) == 1
    assert entries[0].status == QueueStatus.PENDING
    snapshot_root = case.job_dir / ".orca_auto_input_snapshots"
    assert {path.name for path in snapshot_root.iterdir()} == {
        entries[0].metadata["execution_snapshot"]["snapshot_namespace"]
    }

    terminal = mark_completed(
        case.runs_root,
        first["queue_id"],
        expected_entry=entries[0],
        expected_task_id=entries[0].task_id,
    )
    assert terminal is not None and terminal.status == QueueStatus.COMPLETED

    second = _submit(case, priority=5)
    assert second["status"] == "queued"
    assert second["job_id"] != first["job_id"]
    assert second["queue_id"] != first["queue_id"]
    generations = list_queue(case.runs_root)
    assert [entry.status for entry in generations] == [
        QueueStatus.COMPLETED,
        QueueStatus.PENDING,
    ]
    namespaces = {
        entry.metadata["execution_snapshot"]["snapshot_namespace"] for entry in generations
    }
    assert len(namespaces) == 2
    assert all(
        (case.job_dir / ".orca_auto_input_snapshots" / namespace).is_dir()
        for namespace in namespaces
    )


def test_cancellation_racing_queued_record_publication_wins_terminally(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()

    @contextmanager
    def cancel_before_publisher_lock(_root: Path, queue_id: str):
        entry = _entry(case, queue_id)
        cancelled = activity.cancel_activity(
            target=entry.queue_id,
            shared_config=str(case.config_path),
        )
        assert cancelled["status"] == "cancelled"
        yield

    monkeypatch.setattr(
        xtb_md_submission,
        "queue_record_publication_lock",
        cancel_before_publisher_lock,
    )
    monkeypatch.setattr(
        xtb_md_submission,
        "_publish_queued_record",
        lambda *_args, **_kwargs: pytest.fail("cancelled publication must not write queued state"),
    )

    result = _submit(case)

    assert result["status"] == "cancelled"
    assert result["reason"] == "submission_cancelled"
    terminal = _entry(case, result["queue_id"])
    assert terminal.status == QueueStatus.CANCELLED
    assert terminal.metadata["_orca_auto_queued_record_sync"] == "aborted"
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"] == {
        "state": "cancelled",
        "reason": "cancel_requested",
        "exit_code": None,
    }


def test_cancelled_submission_publisher_does_not_overwrite_immediate_replacement(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()
    replacement_result: dict[str, Any] = {}
    intercepted = False

    @contextmanager
    def cancel_then_resubmit(_root: Path, queue_id: str):
        nonlocal intercepted
        if intercepted:
            yield
            return
        intercepted = True
        current = _entry(case, queue_id)
        cancelled = activity.cancel_activity(
            target=current.queue_id,
            shared_config=str(case.config_path),
        )
        assert cancelled["status"] == "cancelled"
        replacement_result.update(_submit(case))
        assert replacement_result["status"] == "queued"
        yield

    monkeypatch.setattr(
        xtb_md_submission,
        "queue_record_publication_lock",
        cancel_then_resubmit,
    )

    cancelled_result = _submit(case)

    assert cancelled_result["status"] == "cancelled"
    old_entry = _entry(case, cancelled_result["queue_id"])
    replacement = _entry(case, replacement_result["queue_id"])
    assert old_entry.status == QueueStatus.CANCELLED
    assert replacement.status == QueueStatus.PENDING
    assert replacement.task_id != old_entry.task_id
    replacement_generation = queue_entry_generation_token(replacement)
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    report = json.loads((case.job_dir / "job_report.json").read_text(encoding="utf-8"))
    for artifact in (state, report):
        assert artifact["status"]["state"] == "queued"
        assert artifact["job"]["id"] == replacement.task_id
        assert artifact["job"]["queue_id"] == replacement.queue_id
        assert artifact["job"]["generation"] == replacement_generation


def test_activity_pending_cancel_publishes_exact_generation_state_report_and_index(
    runtime_case_factory,
) -> None:
    case = runtime_case_factory()
    submitted = _submit(case)
    assert submitted["status"] == "queued"
    queued = _entry(case, submitted["queue_id"])
    expected_generation = queue_entry_generation_token(queued)

    payload = activity.cancel_activity(
        target=queued.task_id,
        shared_config=str(case.config_path),
    )

    assert payload["status"] == "cancelled"
    assert payload["engine"] == "xtb_md"
    terminal = _entry(case, queued.queue_id)
    assert terminal.status == QueueStatus.CANCELLED
    assert terminal.task_id == queued.task_id
    assert queue_entry_generation_token(terminal) == expected_generation

    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    report = json.loads((case.job_dir / "job_report.json").read_text(encoding="utf-8"))
    for artifact in (state, report):
        assert artifact["status"] == {
            "state": "cancelled",
            "reason": "cancel_requested",
            "exit_code": None,
        }
        assert artifact["job"]["id"] == queued.task_id
        assert artifact["job"]["queue_id"] == queued.queue_id
        assert artifact["job"]["generation"] == expected_generation

    cfg = load_xtb_md_config(str(case.config_path))
    indexed = [
        (root, record)
        for root, record in list_job_records_for_cfg(cfg)
        if record.job_id == queued.task_id
    ]
    assert len(indexed) == 1
    index_root, record = indexed[0]
    assert index_root == case.runs_root.resolve()
    assert record.status == "cancelled"
    assert record.app_name == "orca_auto_xtb_md"
    assert Path(record.latest_known_path) == case.job_dir.resolve()


def test_submission_publication_rejects_job_directory_symlink_rebinding(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()
    moved_job_dir = case.root / "moved-water-md"

    @contextmanager
    def move_before_publisher_lock(_root: Path, _queue_id: str):
        case.job_dir.rename(moved_job_dir)
        case.job_dir.symlink_to(moved_job_dir, target_is_directory=True)
        yield

    monkeypatch.setattr(
        xtb_md_submission,
        "queue_record_publication_lock",
        move_before_publisher_lock,
    )

    result = _submit(case)

    assert result["status"] == "failed"
    assert result["reason"] == "submission_publication_failed"
    assert "missing, replaced, or contains a symlink" in result["stderr"]
    terminal = _entry(case, result["queue_id"])
    assert terminal.status == QueueStatus.FAILED
    assert not (moved_job_dir / "job_state.json").exists()
    assert not (moved_job_dir / "job_report.json").exists()
    assert not (moved_job_dir / "job_report.md").exists()
    assert not (moved_job_dir / ".orca_auto_xtb_md_executions").exists()


def test_worker_rejects_moved_job_rebound_outside_runs_root_without_writes(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    artifact_names = ("job_state.json", "job_report.json", "job_report.md")
    queued_artifacts = {name: (case.job_dir / name).read_bytes() for name in artifact_names}
    moved_job_dir = case.root / "moved-water-md"
    case.job_dir.rename(moved_job_dir)
    case.job_dir.symlink_to(moved_job_dir, target_is_directory=True)
    claimed = dequeue_entry_if_pending(case.runs_root, queued.queue_id, expected_entry=queued)
    assert claimed is not None and claimed.status == QueueStatus.RUNNING
    token = _reserve_managed_slot(case, claimed)

    assert _run_claimed_entry(case, claimed, token, monkeypatch) == 1
    assert release_slot(case.admission_root, token) is True

    failed = _entry(case, submitted["queue_id"])
    assert failed.status == QueueStatus.FAILED
    assert "missing, replaced, or contains a symlink" in failed.error
    assert not (moved_job_dir / ".orca_auto_xtb_md_executions").exists()
    assert {
        name: (moved_job_dir / name).read_bytes() for name in artifact_names
    } == queued_artifacts
    state = json.loads((moved_job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "queued"


def test_execution_uses_immutable_snapshot_after_source_mutation_and_cannot_retry(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case, priority=2)
    queued = _entry(case, submitted["queue_id"])
    snapshot = queued.metadata["execution_snapshot"]
    geometry_snapshot = Path(snapshot["input_snapshots"]["geometry"]["snapshot_path"])
    geometry_payload = geometry_snapshot.read_bytes()

    (case.job_dir / "water.xyz").write_text("source changed after submit\n", encoding="utf-8")
    (case.job_dir / "xtb_md_job.yaml").write_text(
        "schema_version: 999\ninput_xyz: changed.xyz\n",
        encoding="utf-8",
    )
    claimed = dequeue_entry_if_pending(case.runs_root, queued.queue_id, expected_entry=queued)
    assert claimed is not None and claimed.status == QueueStatus.RUNNING
    token = _reserve_managed_slot(case, claimed)

    assert _run_claimed_entry(case, claimed, token, monkeypatch) == 0
    idle = get_slot(case.admission_root, token)
    assert idle is not None and idle.engine_process_state == "idle"
    assert release_slot(case.admission_root, token) is True

    completed = _entry(case, submitted["queue_id"])
    assert completed.status == QueueStatus.COMPLETED
    execution_dir = Path(completed.metadata["execution_dir"])
    assert execution_dir.parent == case.job_dir / ".orca_auto_xtb_md_executions"
    assert (execution_dir / "water.xyz").read_bytes() == geometry_payload
    assert geometry_snapshot.read_bytes() == geometry_payload
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "completed"
    assert state["engine"] == "xtb_md"
    assert state["engine_payload"]["attempt"] == 1
    assert state["engine_payload"]["max_attempts"] == 1
    assert state["engine_payload"]["retry_supported"] is False
    assert state["engine_payload"]["resume_supported"] is False
    assert state["job"]["generation"] == queue_entry_generation_token(completed)
    report = json.loads((case.job_dir / "job_report.json").read_text(encoding="utf-8"))
    assert report["job"]["generation"] == queue_entry_generation_token(completed)

    repeated = run_xtb_md_attempt(
        load_xtb_md_config(str(case.config_path)),
        completed,
        execution_snapshot=snapshot,
        admission_root=case.admission_root,
        admission_token="no-second-slot",
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
    )
    assert repeated.status == "failed"
    assert "generation already exists" in repeated.reason
    assert "cannot retry" in repeated.reason
    assert [path.name for path in execution_dir.parent.iterdir()] == [completed.task_id]


def test_runner_rejects_job_directory_rebinding_during_engine_execution(
    runtime_case_factory,
) -> None:
    case = runtime_case_factory(mode="slow")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    moved_job_dir = case.root / "moved-during-execution"

    def move_after_start(
        _execution_dir: str,
        _started_at: str,
        _command: tuple[str, ...],
    ) -> None:
        case.job_dir.rename(moved_job_dir)
        case.job_dir.symlink_to(moved_job_dir, target_is_directory=True)

    result = run_xtb_md_attempt(
        load_xtb_md_config(str(case.config_path)),
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
        on_started=move_after_start,
    )

    assert result.status == "failed"
    assert result.reason.startswith("job_directory_identity_changed:")
    assert "missing, replaced, or contains a symlink" in result.reason
    assert result.artifacts == {}
    idle = get_slot(case.admission_root, token)
    assert idle is not None and idle.engine_process_state == "idle"
    assert release_slot(case.admission_root, token) is True


def test_runner_rejects_tampered_snapshot_before_engine_launch(runtime_case_factory) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    snapshot = queued.metadata["execution_snapshot"]
    geometry_snapshot = Path(snapshot["input_snapshots"]["geometry"]["snapshot_path"])
    tampered_payload = bytearray(geometry_snapshot.read_bytes())
    tampered_payload[-2] = ord("6") if tampered_payload[-2] != ord("6") else ord("7")
    geometry_snapshot.chmod(0o600)
    geometry_snapshot.write_bytes(tampered_payload)

    result = run_xtb_md_attempt(
        load_xtb_md_config(str(case.config_path)),
        queued,
        execution_snapshot=snapshot,
        admission_root=case.admission_root,
        admission_token="not-used-before-validation",
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
    )

    assert result.status == "failed"
    assert "failed digest verification" in result.reason
    execution_dir = Path(result.execution_dir)
    assert execution_dir.is_dir()
    assert not (execution_dir / "xtb.stdout.log").exists()
    assert not (execution_dir / "xtbmdok").exists()


def test_false_success_is_terminal_failure_not_completion(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="false_success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    claimed = dequeue_entry_if_pending(case.runs_root, queued.queue_id, expected_entry=queued)
    assert claimed is not None
    token = _reserve_managed_slot(case, claimed)

    assert _run_claimed_entry(case, claimed, token, monkeypatch) == 1
    assert release_slot(case.admission_root, token) is True

    failed = _entry(case, submitted["queue_id"])
    assert failed.status == QueueStatus.FAILED
    assert "fatal marker" in failed.error
    assert "MD is unstable, emergency exit".casefold() in failed.error.casefold()
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "failed"
    assert "fatal marker" in state["status"]["reason"]


def test_running_cancellation_is_terminal_and_does_not_retry(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="slow")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    claimed = dequeue_entry_if_pending(case.runs_root, queued.queue_id, expected_entry=queued)
    assert claimed is not None
    cancelling = request_cancel(
        case.runs_root,
        claimed.queue_id,
        expected_entry=claimed,
    )
    assert cancelling is not None and cancelling.cancel_requested is True
    token = _reserve_managed_slot(case, cancelling)

    assert _run_claimed_entry(case, cancelling, token, monkeypatch) == 0
    assert release_slot(case.admission_root, token) is True

    cancelled = _entry(case, submitted["queue_id"])
    assert cancelled.status == QueueStatus.CANCELLED
    assert cancelled.error == "cancel_requested"
    assert cancelled.cancel_requested is False
    assert cancelled.metadata["attempt"] == 1
    assert cancelled.metadata["retry_supported"] is False
    assert cancelled.metadata["resume_supported"] is False
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "cancelled"
    assert state["engine_payload"]["max_attempts"] == 1


def test_shutdown_terminates_engine_and_fails_without_retry(runtime_case_factory) -> None:
    case = runtime_case_factory(mode="slow")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)

    result = run_xtb_md_attempt(
        load_xtb_md_config(str(case.config_path)),
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: True,
    )

    assert result.status == "failed"
    assert result.reason == "worker_shutdown_no_retry"
    assert result.exit_code is not None
    idle = get_slot(case.admission_root, token)
    assert idle is not None and idle.engine_process_state == "idle"
    assert release_slot(case.admission_root, token) is True
    assert list((case.job_dir / ".orca_auto_xtb_md_executions").iterdir())


@pytest.mark.parametrize("ensemble", ["nvt", "nve"])
def test_real_xtb_671_two_step_acceptance_when_configured(
    runtime_case_factory,
    valid_manifest_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    ensemble: str,
) -> None:
    executable_text = os.environ.get("XTB_MD_REAL_EXECUTABLE", "").strip()
    if not executable_text:
        pytest.skip("set XTB_MD_REAL_EXECUTABLE to run the real xTB acceptance")
    executable = Path(executable_text).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        pytest.fail(f"XTB_MD_REAL_EXECUTABLE is not executable: {executable}")

    case = runtime_case_factory(
        mode="success",
        manifest_payload={**valid_manifest_payload, "ensemble": ensemble},
    )
    config = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    config["workflow"]["paths"]["xtb_executable"] = str(executable)
    case.config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    submitted = _submit(case)
    assert submitted["status"] == "queued", submitted
    queued = _entry(case, submitted["queue_id"])
    claimed = dequeue_entry_if_pending(case.runs_root, queued.queue_id, expected_entry=queued)
    assert claimed is not None
    token = _reserve_managed_slot(case, claimed)

    assert _run_claimed_entry(case, claimed, token, monkeypatch) == 0
    assert release_slot(case.admission_root, token) is True
    completed = _entry(case, submitted["queue_id"])
    assert completed.status == QueueStatus.COMPLETED
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "completed"
    assert state["engine_payload"]["ensemble"] == ensemble
    assert state["engine_payload"]["completed_steps"] == 2
    assert state["engine_payload"]["trajectory_frames"] == 2
