from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from orca_auto import cli_handlers as cli_run_dir
from orca_auto.cli import main as cli_main
from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.core.admission import get_slot, list_slots, release_slot, reserve_slot
from orca_auto.core.commands.run_dir import use_run_dir_publication_guard
from orca_auto.core.config.engines import load_xtb_md_config
from orca_auto.core.config.scratch import ScratchConfig
from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QueueStatus,
    dequeue_entry_if_pending,
    list_queue,
    mark_completed,
    queue_entry_is_claimable,
    queue_record_sync_metadata,
    request_cancel,
)
from orca_auto.core.queue import enqueue_publication as core_enqueue_publication
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.processes import worker_pid_file_path
from orca_auto.core.queue.store import mutate_entries
from orca_auto.flow import activity
from orca_auto.xtb_md import execution, queue_runtime
from orca_auto.xtb_md import runner as xtb_md_runner
from orca_auto.xtb_md import submission as xtb_md_submission
from orca_auto.xtb_md.engine import ENGINE_DEFINITION
from orca_auto.xtb_md.job_locations import list_job_records_for_cfg
from orca_auto.xtb_md.runner import run_xtb_md_attempt
from orca_auto.xtb_md.submission import APP_NAME, submit_job_dir


def _submit(case: Any, *, priority: int = 10) -> dict[str, Any]:
    return submit_job_dir(
        job_dir=str(case.job_dir),
        priority=priority,
        config_path=str(case.config_path),
    )


def _submit_via_public_cli(
    case: Any,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    assert (
        cli_main(
            [
                "run-dir",
                str(case.job_dir),
                "--config",
                str(case.config_path),
                "--json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    assert payload["job_dir"] == str(case.job_dir.resolve())
    return payload


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


def _scratch_enabled_config(
    case: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Path]:
    fake_shm = case.root / "shm"
    fake_shm.mkdir()
    scratch_root = fake_shm / "orca_auto"
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", fake_shm)
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: 2**63)
    cfg = replace(
        load_xtb_md_config(str(case.config_path)),
        scratch=ScratchConfig(root=str(scratch_root), min_free_gb=1),
    )
    return cfg, scratch_root


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


def test_public_run_dir_guard_aborts_xtb_md_before_durable_queue_commit(
    runtime_case_factory,
) -> None:
    case = runtime_case_factory()
    stages: list[str] = []

    def reject_publication(stage: str) -> None:
        stages.append(stage)
        raise RuntimeError("run-dir target moved into reserved smoke results")

    with use_run_dir_publication_guard(reject_publication):
        result = _submit(case)

    assert result["status"] == "failed"
    assert result["reason"] == "submission_failed"
    assert stages == ["xTB-MD target mutation preflight"]
    assert list_queue(case.runs_root) == []
    assert not (case.runs_root / "queue.json").exists()
    assert not (case.runs_root / "job_locations.json").exists()
    assert list_job_records_for_cfg(load_xtb_md_config(str(case.config_path))) == []
    for artifact_name in ("job_state.json", "job_report.json", "job_report.md"):
        assert not (case.job_dir / artifact_name).exists()
    snapshot_root = case.job_dir / ".orca_auto_input_snapshots"
    assert not snapshot_root.exists() or list(snapshot_root.iterdir()) == []


@pytest.mark.parametrize(
    "guard_error",
    [
        pytest.param(
            RuntimeError("run-dir target moved into reserved smoke results"),
            id="runtime-error",
        ),
        pytest.param(
            KeyboardInterrupt("run-dir guard interrupted after commit"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("run-dir guard exited after commit"),
            id="system-exit",
        ),
    ],
)
def test_public_run_dir_guard_compensates_xtb_md_post_commit_rejection(
    runtime_case_factory,
    guard_error: BaseException,
) -> None:
    case = runtime_case_factory()
    stages: list[str] = []

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise guard_error

    with use_run_dir_publication_guard(reject_after_commit):
        result = _submit(case)

    assert result["status"] == "failed"
    assert result["reason"] == "submission_failed"
    assert str(guard_error) in result["stderr"]
    assert stages == [
        "xTB-MD target mutation preflight",
        "xTB-MD durable queue pre-commit",
        "xTB-MD durable queue post-commit",
    ]
    assert list_queue(case.runs_root) == []
    assert not (case.runs_root / "job_locations.json").exists()
    assert list_job_records_for_cfg(load_xtb_md_config(str(case.config_path))) == []
    for artifact_name in ("job_state.json", "job_report.json", "job_report.md"):
        assert not (case.job_dir / artifact_name).exists()
    snapshot_root = case.job_dir / ".orca_auto_input_snapshots"
    assert not snapshot_root.exists() or list(snapshot_root.iterdir()) == []


@pytest.mark.parametrize(
    "compensation_error",
    [
        pytest.param(
            OSError("queue compensation write failed before replace"),
            id="os-error",
        ),
        pytest.param(
            KeyboardInterrupt("queue compensation interrupted before replace"),
            id="keyboard-interrupt",
        ),
        pytest.param(
            SystemExit("queue compensation exited before replace"),
            id="system-exit",
        ),
    ],
)
def test_xtb_md_compensation_failure_fences_row_without_publication(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
    compensation_error: BaseException,
) -> None:
    case = runtime_case_factory()
    stages: list[str] = []
    save_count = 0
    original_save = queue_store.save_entries

    def fail_compensation_before_replace(root: Path, entries: Any) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise compensation_error
        original_save(root, entries)

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise RuntimeError("run-dir target moved into reserved smoke results")

    def reject_publication(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("guard-origin compensation failure must not publish a queued record")

    monkeypatch.setattr(queue_store, "save_entries", fail_compensation_before_replace)
    monkeypatch.setattr(xtb_md_submission, "publish_queued_record", reject_publication)

    with use_run_dir_publication_guard(reject_after_commit):
        result = _submit(case)

    assert result["status"] == "failed"
    assert result["reason"] == "queue_enqueue_outcome_unknown"
    assert "run-dir target moved into reserved smoke results" in result["stderr"]
    assert "queue compensation outcome=not_restored" in result["stderr"]
    assert str(compensation_error) in result["stderr"]
    assert stages == [
        "xTB-MD target mutation preflight",
        "xTB-MD durable queue pre-commit",
        "xTB-MD durable queue post-commit",
    ]
    assert save_count == 3
    [entry] = list_queue(case.runs_root)
    assert entry.status == QueueStatus.FAILED
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert "queue_after_commit_guard_failed" in entry.error
    assert queue_entry_is_claimable(entry) is False
    assert not (case.runs_root / "job_locations.json").exists()
    assert list_job_records_for_cfg(load_xtb_md_config(str(case.config_path))) == []
    for artifact_name in ("job_state.json", "job_report.json", "job_report.md"):
        assert not (case.job_dir / artifact_name).exists()
    snapshot_namespace = entry.metadata["execution_snapshot"]["snapshot_namespace"]
    assert (case.job_dir / ".orca_auto_input_snapshots" / snapshot_namespace).is_dir()


def test_public_cli_rechecks_before_first_xtb_md_target_mutation(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()
    normal_job = case.job_dir
    reserved_job = case.runs_root / SMOKE_RESULTS_DIRNAME / "batch" / normal_job.name
    reserved_job.parent.mkdir(parents=True)
    manifest_payload = (normal_job / "xtb_md_job.yaml").read_bytes()
    geometry_payload = (normal_job / "water.xyz").read_bytes()
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0

    def move_after_central_dispatch(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 3:
            normal_job.rename(reserved_job)

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        move_after_central_dispatch,
    )

    assert (
        cli_main(
            [
                "run-dir",
                str(normal_job),
                "--config",
                str(case.config_path),
                "--json",
            ]
        )
        == 1
    )
    assert gate_count == 3
    assert (reserved_job / "xtb_md_job.yaml").read_bytes() == manifest_payload
    assert (reserved_job / "water.xyz").read_bytes() == geometry_payload
    assert list_queue(case.runs_root) == []
    assert not (reserved_job / ".orca_auto_input_snapshots").exists()


def test_public_cli_compensates_xtb_md_snapshot_after_precommit_relocation(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()
    normal_job = case.job_dir
    reserved_job = case.runs_root / SMOKE_RESULTS_DIRNAME / "batch" / normal_job.name
    reserved_job.parent.mkdir(parents=True)
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0

    def move_after_queue_precommit(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 5:
            normal_job.rename(reserved_job)

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        move_after_queue_precommit,
    )

    assert (
        cli_main(
            [
                "run-dir",
                str(normal_job),
                "--config",
                str(case.config_path),
                "--json",
            ]
        )
        == 1
    )
    assert gate_count == 5
    assert list_queue(case.runs_root) == []
    assert not (case.runs_root / "job_locations.json").exists()
    assert not (reserved_job / ".orca_auto_input_snapshots").exists()
    for artifact_name in ("job_state.json", "job_report.json", "job_report.md"):
        assert not (reserved_job / artifact_name).exists()


def test_public_cli_rejects_xtb_md_namespace_replacement_after_preflight(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()
    normal_job = case.job_dir
    moved_original = case.runs_root / "moved-original"
    manifest_payload = (normal_job / "xtb_md_job.yaml").read_bytes()
    geometry_path = normal_job / "water.xyz"
    original_geometry = geometry_path.read_text(encoding="utf-8")
    replacement_lines = original_geometry.splitlines()
    replacement_lines[1] = "replacement"
    replacement_geometry = "\n".join(replacement_lines) + "\n"
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0

    def replace_after_mutation_preflight(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 4:
            normal_job.rename(moved_original)
            normal_job.mkdir()
            (normal_job / "xtb_md_job.yaml").write_bytes(manifest_payload)
            (normal_job / "water.xyz").write_text(replacement_geometry, encoding="utf-8")

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        replace_after_mutation_preflight,
    )

    assert (
        cli_main(
            [
                "run-dir",
                str(normal_job),
                "--config",
                str(case.config_path),
                "--json",
            ]
        )
        == 1
    )
    assert gate_count == 4
    assert list_queue(case.runs_root) == []
    assert (moved_original / "water.xyz").read_text(encoding="utf-8") == original_geometry
    assert (normal_job / "water.xyz").read_text(encoding="utf-8") == replacement_geometry
    for job_dir in (moved_original, normal_job):
        assert not (job_dir / ".orca_auto_input_snapshots").exists()


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
        core_enqueue_publication,
        "queue_record_publication_lock",
        cancel_before_publisher_lock,
    )
    monkeypatch.setattr(
        xtb_md_submission,
        "publish_queued_record",
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
        core_enqueue_publication,
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
        core_enqueue_publication,
        "queue_record_publication_lock",
        move_before_publisher_lock,
    )

    result = _submit(case)

    # The rebinding is caught by the publish guard: the row stays durably
    # queued but unpublished (REPAIR_PENDING), and the worker repair pass
    # keeps refusing to publish into the rebound directory, so the row can
    # never be claimed and nothing is ever written through the symlink.
    assert result["status"] == "queued"
    assert result["publication"] == "deferred"
    assert any("missing, replaced, or contains a symlink" in w for w in result["warnings"])
    parked = _entry(case, result["queue_id"])
    assert parked.status == QueueStatus.PENDING
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert queue_entry_is_claimable(parked) is False
    cfg = load_xtb_md_config(str(case.config_path))
    assert queue_runtime._repair_queued_publication(cfg, case.runs_root, parked) is False
    still_parked = _entry(case, result["queue_id"])
    assert still_parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert queue_entry_is_claimable(still_parked) is False
    assert not (moved_job_dir / "job_state.json").exists()
    assert not (moved_job_dir / "job_report.json").exists()
    assert not (moved_job_dir / "job_report.md").exists()
    assert not (moved_job_dir / ".orca_auto_xtb_md_executions").exists()


def test_publication_failure_defers_to_worker_repair_roundtrip(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()

    def fail_publish(_cfg: Any, _entry: Any) -> None:
        raise OSError("queued artifact write failed transiently")

    monkeypatch.setattr(xtb_md_submission, "publish_queued_record", fail_publish)

    result = _submit(case)

    assert result["status"] == "queued"
    assert result["publication"] == "deferred"
    assert any("worker repair will publish" in w for w in result["warnings"])
    parked = _entry(case, result["queue_id"])
    assert parked.status == QueueStatus.PENDING
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert queue_entry_is_claimable(parked) is False
    assert not (case.job_dir / "job_state.json").exists()

    # The worker's pre-claim repair pass (queue_runtime binds the real
    # publisher, unaffected by the submission-module patch) publishes the
    # queued record and completes the lease.
    cfg = load_xtb_md_config(str(case.config_path))
    assert queue_runtime._repair_queued_publication(cfg, case.runs_root, parked) is True
    repaired = _entry(case, result["queue_id"])
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(repaired) is True
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "queued"
    assert state["job"]["queue_id"] == repaired.queue_id


def test_enqueue_result_lost_after_commit_recovers_to_repair_pending(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()
    real_enqueue = core_enqueue_publication.enqueue

    def commit_then_lose(*args: Any, **kwargs: Any) -> Any:
        real_enqueue(*args, **kwargs)
        raise OSError("durability barrier failed after the enqueue committed")

    monkeypatch.setattr(core_enqueue_publication, "enqueue", commit_then_lose)
    monkeypatch.setattr(
        xtb_md_submission,
        "publish_queued_record",
        lambda *_args, **_kwargs: pytest.fail(
            "a recovered enqueue must defer publication to worker repair"
        ),
    )

    result = _submit(case)

    # The strict recovery matched the exact committed row (token is necessary
    # but not sufficient) and parked it for the worker repair pass instead of
    # publishing after an unknown failure.
    assert result["status"] == "queued"
    assert result["publication"] == "deferred"
    assert any("parked for worker repair" in w for w in result["warnings"])
    parked = _entry(case, result["queue_id"])
    assert parked.status == QueueStatus.PENDING
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert queue_entry_is_claimable(parked) is False
    snapshot_namespace = parked.metadata["execution_snapshot"]["snapshot_namespace"]
    assert (case.job_dir / ".orca_auto_input_snapshots" / snapshot_namespace).is_dir()

    cfg = load_xtb_md_config(str(case.config_path))
    assert queue_runtime._repair_queued_publication(cfg, case.runs_root, parked) is True
    repaired = _entry(case, result["queue_id"])
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert queue_entry_is_claimable(repaired) is True


def test_publication_complete_shortcircuit_requires_own_token(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory()

    @contextmanager
    def foreign_complete_before_publisher_lock(root: Path, queue_id: str):
        def fence(entries: list[Any]) -> tuple[None, bool]:
            for index, current in enumerate(entries):
                if current.queue_id != queue_id:
                    continue
                metadata = dict(current.metadata)
                metadata.update(
                    queue_record_sync_metadata(
                        QUEUE_RECORD_SYNC_COMPLETE,
                        token="foreign-lease-token",
                        owner_pid=0,
                    )
                )
                entries[index] = replace(current, metadata=metadata)
                return None, True
            return None, False

        mutate_entries(root, fence)
        yield

    monkeypatch.setattr(
        core_enqueue_publication,
        "queue_record_publication_lock",
        foreign_complete_before_publisher_lock,
    )
    monkeypatch.setattr(
        xtb_md_submission,
        "publish_queued_record",
        lambda *_args, **_kwargs: pytest.fail(
            "a COMPLETE lease owned by another publisher must not be republished"
        ),
    )

    result = _submit(case)

    # A COMPLETE written by a different lease is ownership loss, never this
    # publisher's own success: the submitter neither publishes over it nor
    # claims it as published.
    assert result["status"] == "queued"
    assert result["publication"] == "deferred"
    assert any("ownership changed" in w for w in result["warnings"])
    current = _entry(case, result["queue_id"])
    assert current.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


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
    assert execution_dir.parent == (case.job_dir / ".orca_auto_xtb_md_executions").resolve()
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


@pytest.mark.parametrize("scratch_enabled", [False, True], ids=["durable", "scratch"])
def test_runner_rejects_job_directory_rebinding_during_engine_execution(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
    scratch_enabled: bool,
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

    cfg = load_xtb_md_config(str(case.config_path))
    scratch_root: Path | None = None
    if scratch_enabled:
        cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)

    result = run_xtb_md_attempt(
        cfg,
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
    if scratch_enabled:
        assert result.engine_payload["scratch_provenance"]["publication_status"] == "unresolved"
        assert scratch_root is not None
        attempts = [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")]
        assert len(attempts) == 1
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


def test_runner_executes_in_scratch_and_publishes_only_canonical_outputs(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)
    observed: dict[str, Any] = {}
    reported: dict[str, Any] = {}
    original_start = xtb_md_runner.start_logged_process

    def observe_start(*args: Any, **kwargs: Any) -> Any:
        observed["command"] = tuple(args[0])
        observed["cwd"] = Path(kwargs["cwd"])
        launched = original_start(*args, **kwargs)
        (observed["cwd"] / "charges").write_bytes(b"x" * 4096)
        return launched

    def observe_running(
        execution_dir: str,
        started_at: str,
        command: tuple[str, ...],
    ) -> None:
        reported.update(
            execution_dir=execution_dir,
            started_at=started_at,
            command=command,
        )

    monkeypatch.setattr(xtb_md_runner, "start_logged_process", observe_start)
    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
        on_started=observe_running,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "completed"
    execution_dir = Path(result.execution_dir)
    process_dir = observed["cwd"]
    assert process_dir.parent == scratch_root
    assert not process_dir.exists()
    assert Path(observed["command"][1]).parent == process_dir
    assert Path(observed["command"][3]).parent == process_dir
    assert reported["execution_dir"] == str(execution_dir)
    assert Path(reported["command"][1]).parent == execution_dir
    assert Path(reported["command"][3]).parent == execution_dir
    assert result.command == reported["command"]
    assert {path.name for path in execution_dir.iterdir()} == {
        ".orca_auto_xtb_md_attempt",
        ".orca_auto_runtime_home",
        "water.xyz",
        "md.inp",
        "xtb.stdout.log",
        "xtb.stderr.log",
        "xtb.trj",
        "mdrestart",
        "xtbmdok",
    }
    assert all(
        Path(identity["path"]).parent == execution_dir for identity in result.artifacts.values()
    )
    provenance = result.engine_payload["scratch_provenance"]
    assert provenance["used"] is True
    assert provenance["filesystem"] == "tmpfs"
    assert provenance["publication_status"] == "committed"
    assert set(provenance["published_files"]) == {
        "xtb.stdout.log",
        "xtb.stderr.log",
        "xtb.trj",
        "mdrestart",
        "xtbmdok",
    }
    assert provenance["omitted_transient_files"] == ["charges"]
    assert provenance["omitted_transient_bytes"] == 4096
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_scratch_false_success_is_published_then_rejected(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="false_success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "failed"
    assert "fatal marker" in result.reason
    assert result.artifacts == {}
    execution_dir = Path(result.execution_dir)
    assert "MD is unstable, emergency exit" in (execution_dir / "xtb.stdout.log").read_text(
        encoding="utf-8"
    )
    assert (execution_dir / "xtb.trj").is_file()
    assert result.engine_payload["scratch_provenance"]["publication_status"] == "committed"
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_scratch_shutdown_publishes_logs_and_cleans_workspace(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="slow")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: True,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "failed"
    assert result.reason == "worker_shutdown_no_retry"
    assert result.engine_payload["scratch_provenance"]["publication_status"] == "committed"
    execution_dir = Path(result.execution_dir)
    assert (execution_dir / "xtb.stdout.log").is_file()
    assert (execution_dir / "xtb.stderr.log").is_file()
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_scratch_cancellation_publishes_logs_and_stays_terminal(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="slow")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: True,
        shutdown_requested=lambda: False,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "cancelled"
    assert result.reason == "cancel_requested"
    assert result.engine_payload["scratch_provenance"]["publication_status"] == "committed"
    execution_dir = Path(result.execution_dir)
    assert (execution_dir / "xtb.stdout.log").is_file()
    assert (execution_dir / "xtb.stderr.log").is_file()
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


def test_scratch_cancellation_never_publishes_an_oversized_log(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="slow")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)
    original_start = xtb_md_runner.start_logged_process

    def start_with_oversized_log(*args: Any, **kwargs: Any) -> Any:
        launched = original_start(*args, **kwargs)
        Path(kwargs["stdout_log"]).write_bytes(b"oversized")
        return launched

    monkeypatch.setattr(xtb_md_runner, "_MAX_LOG_BYTES", 8)
    monkeypatch.setattr(xtb_md_runner, "start_logged_process", start_with_oversized_log)

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: True,
        shutdown_requested=lambda: False,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "failed"
    assert result.reason == "output_policy_violation:xtb.stdout.log_size_limit_exceeded"
    assert result.engine_payload["scratch_provenance"] == {
        "used": True,
        "filesystem": "tmpfs",
        "publication_status": "unresolved",
    }
    attempts = [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")]
    assert len(attempts) == 1
    assert (attempts[0] / "xtb.stdout.log").stat().st_size > 8
    assert not (Path(result.execution_dir) / "xtb.stdout.log").exists()


def test_scratch_input_mutation_fails_closed_and_retains_unresolved_workspace(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)

    def mutate_durable_input(
        execution_dir: str,
        _started_at: str,
        _command: tuple[str, ...],
    ) -> None:
        geometry = Path(execution_dir) / "water.xyz"
        geometry.chmod(0o600)
        geometry.write_text("3\nchanged\nO 0 0 0\nH 0 0 1\nH 0 1 0\n", encoding="utf-8")

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
        on_started=mutate_durable_input,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "failed"
    assert "Durable engine input changed during scratch run" in result.reason
    assert result.engine_payload["scratch_provenance"] == {
        "used": True,
        "filesystem": "tmpfs",
        "publication_status": "unresolved",
    }
    attempts = [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")]
    assert len(attempts) == 1
    assert (attempts[0] / "xtb.trj").is_file()
    assert not (Path(result.execution_dir) / "xtb.trj").exists()


def test_scratch_oversized_log_is_not_published_to_durable_storage(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)
    monkeypatch.setattr(xtb_md_runner, "_MAX_LOG_BYTES", 8)

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "failed"
    assert result.reason == "output_policy_violation:xtb.stdout.log_size_limit_exceeded"
    assert result.engine_payload["scratch_provenance"] == {
        "used": True,
        "filesystem": "tmpfs",
        "publication_status": "unresolved",
    }
    attempts = [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")]
    assert len(attempts) == 1
    assert (attempts[0] / "xtb.stdout.log").stat().st_size > 8
    execution_dir = Path(result.execution_dir)
    assert not (execution_dir / "xtb.stdout.log").exists()
    assert not (execution_dir / "xtb.trj").exists()


def test_scratch_headroom_failure_never_falls_back_to_durable_execution(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case_factory(mode="success")
    submitted = _submit(case)
    queued = _entry(case, submitted["queue_id"])
    token = _reserve_managed_slot(case, queued)
    cfg, scratch_root = _scratch_enabled_config(case, monkeypatch)
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: 1)
    monkeypatch.setattr(
        xtb_md_runner,
        "start_logged_process",
        lambda *_args, **_kwargs: pytest.fail("xTB must not start after scratch admission fails"),
    )

    result = run_xtb_md_attempt(
        cfg,
        queued,
        execution_snapshot=queued.metadata["execution_snapshot"],
        admission_root=case.admission_root,
        admission_token=token,
        should_cancel=lambda: False,
        shutdown_requested=lambda: False,
    )

    assert release_slot(case.admission_root, token) is True
    assert result.status == "failed"
    assert result.reason.startswith(
        "EngineScratchError:engine scratch cannot guarantee RAM headroom"
    )
    execution_dir = Path(result.execution_dir)
    assert execution_dir.is_dir()
    assert not (execution_dir / "xtb.stdout.log").exists()
    assert [path for path in scratch_root.iterdir() if path.name.startswith("attempt-")] == []


@pytest.mark.parametrize("ensemble", ["nvt", "nve"], ids=["nvt", "nve"])
def test_fake_xtb_md_nvt_nve_smoke(
    runtime_case_factory,
    valid_manifest_payload: dict[str, Any],
    ensemble: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = runtime_case_factory(
        mode="success",
        manifest_payload={**valid_manifest_payload, "ensemble": ensemble},
    )
    submitted = _submit_via_public_cli(case, capsys)
    assert submitted["status"] == "queued"
    queued = _entry(case, submitted["queue_id"])
    assert queued.status == QueueStatus.PENDING
    assert queued.metadata["ensemble"] == ensemble

    cfg = load_xtb_md_config(str(case.config_path))
    worker = queue_runtime.QueueWorker(
        cfg,
        str(case.config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0

    completed = _entry(case, submitted["queue_id"])
    assert completed.queue_id == queued.queue_id
    assert completed.task_id == queued.task_id
    assert completed.status == QueueStatus.COMPLETED
    assert completed.error == ""
    assert completed.metadata["attempt"] == 1
    assert completed.metadata["retry_supported"] is False
    assert completed.metadata["resume_supported"] is False
    assert list_slots(case.admission_root) == []
    assert not worker_pid_file_path(
        case.runs_root,
        ENGINE_DEFINITION.queue_functions.worker_pid_file_name,
    ).exists()

    execution_dir = Path(completed.metadata["execution_dir"])
    assert execution_dir.parent == (case.job_dir / ".orca_auto_xtb_md_executions").resolve()
    md_input = (execution_dir / "md.inp").read_text(encoding="utf-8")
    assert f"nvt={'true' if ensemble == 'nvt' else 'false'}" in md_input
    assert "restart=false" in md_input

    terminal_artifacts = completed.metadata["terminal_artifacts"]
    expected_names = {
        "trajectory": "xtb.trj",
        "checkpoint": "mdrestart",
        "success_marker": "xtbmdok",
        "stdout_log": "xtb.stdout.log",
        "stderr_log": "xtb.stderr.log",
    }
    assert set(terminal_artifacts) == set(expected_names)
    for artifact_name, filename in expected_names.items():
        identity = terminal_artifacts[artifact_name]
        artifact_path = Path(identity["path"])
        assert artifact_path == execution_dir / filename
        assert artifact_path.is_file()
        assert identity["size_bytes"] == artifact_path.stat().st_size
        assert len(identity["sha256"]) == 64
    assert (execution_dir / "xtb.trj").read_text(encoding="utf-8").count("water snapshot") == 2
    assert "normal exit of md()" in (execution_dir / "xtb.stdout.log").read_text(encoding="utf-8")
    assert "normal termination of xtb" in (execution_dir / "xtb.stderr.log").read_text(
        encoding="utf-8"
    )

    generation = queue_entry_generation_token(completed)
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    report = json.loads((case.job_dir / "job_report.json").read_text(encoding="utf-8"))
    for artifact in (state, report):
        assert artifact["schema_version"] == 1
        assert artifact["engine"] == "xtb_md"
        assert artifact["status"] == {
            "state": "completed",
            "reason": "completed",
            "exit_code": 0,
        }
        assert artifact["job"]["id"] == completed.task_id
        assert artifact["job"]["queue_id"] == completed.queue_id
        assert artifact["job"]["generation"] == generation
        assert artifact["engine_payload"]["ensemble"] == ensemble
        assert artifact["engine_payload"]["completed_steps"] == 2
        assert artifact["engine_payload"]["trajectory_frames"] == 2
        assert artifact["engine_payload"]["atom_count"] == 3
        assert "--norestart" in artifact["engine_payload"]["command"]
    report_md = (case.job_dir / "job_report.md").read_text(encoding="utf-8")
    assert "Status: `completed`" in report_md
    assert f"ensemble: `{ensemble}`" in report_md


def test_false_success_is_terminal_failure_not_completion(
    runtime_case_factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = runtime_case_factory(mode="false_success")
    submitted = _submit_via_public_cli(case, capsys)
    queued = _entry(case, submitted["queue_id"])
    claimed = dequeue_entry_if_pending(case.runs_root, queued.queue_id, expected_entry=queued)
    assert claimed is not None
    token = _reserve_managed_slot(case, claimed)

    assert _run_claimed_entry(case, claimed, token, monkeypatch) == 1
    idle = get_slot(case.admission_root, token)
    assert idle is not None and idle.engine_process_state == "idle"
    assert release_slot(case.admission_root, token) is True
    assert list_slots(case.admission_root) == []

    failed = _entry(case, submitted["queue_id"])
    assert failed.status == QueueStatus.FAILED
    assert "fatal marker" in failed.error
    assert "MD is unstable, emergency exit".casefold() in failed.error.casefold()
    assert failed.metadata["attempt"] == 1
    assert failed.metadata["retry_supported"] is False
    assert failed.metadata["resume_supported"] is False
    execution_dir = Path(failed.metadata["execution_dir"])
    assert execution_dir.parent == (case.job_dir / ".orca_auto_xtb_md_executions").resolve()
    assert (execution_dir / "xtb.trj").exists()
    assert (execution_dir / "mdrestart").exists()
    assert (execution_dir / "xtbmdok").exists()
    stdout_log = (execution_dir / "xtb.stdout.log").read_text(encoding="utf-8")
    assert "MD is unstable, emergency exit" in stdout_log
    state = json.loads((case.job_dir / "job_state.json").read_text(encoding="utf-8"))
    assert state["status"]["state"] == "failed"
    assert "fatal marker" in state["status"]["reason"]
    assert state["job"]["generation"] == queue_entry_generation_token(failed)
    assert state["engine_payload"]["retry_supported"] is False
    assert state["engine_payload"]["resume_supported"] is False
    report = json.loads((case.job_dir / "job_report.json").read_text(encoding="utf-8"))
    assert report["status"] == state["status"]
    assert report["job"]["generation"] == state["job"]["generation"]
    report_md = (case.job_dir / "job_report.md").read_text(encoding="utf-8")
    assert "Status: `failed`" in report_md
    assert "fatal marker" in report_md


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
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable_text = os.environ.get("XTB_MD_REAL_EXECUTABLE", "").strip()
    if not executable_text:
        pytest.skip("set XTB_MD_REAL_EXECUTABLE to run the real xTB acceptance")
    executable = Path(executable_text).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        pytest.fail(f"XTB_MD_REAL_EXECUTABLE is not executable: {executable}")

    case = runtime_case_factory(
        mode="success",
        manifest_payload={
            **valid_manifest_payload,
            "ensemble": ensemble,
            "resources": {"max_cores": 1, "max_memory_gb": 2},
        },
    )
    config = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    config["workflow"]["paths"]["xtb_executable"] = str(executable)
    scratch_root_text = os.environ.get("XTB_MD_REAL_SCRATCH_ROOT", "").strip()
    if scratch_root_text:
        scratch_min_free_gb = int(os.environ.get("XTB_MD_REAL_SCRATCH_MIN_FREE_GB", "8").strip())
        config.setdefault("orca", {}).setdefault("runtime", {}).update(
            {
                "scratch_root": scratch_root_text,
                "scratch_min_free_gb": scratch_min_free_gb,
            }
        )
    case.config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    observed_process: dict[str, Any] = {}
    original_start = xtb_md_runner.start_logged_process

    def observe_start(*args: Any, **kwargs: Any) -> Any:
        observed_process["command"] = tuple(args[0])
        observed_process["cwd"] = Path(kwargs["cwd"])
        return original_start(*args, **kwargs)

    monkeypatch.setattr(xtb_md_runner, "start_logged_process", observe_start)
    submitted = _submit_via_public_cli(case, capsys)
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
    assert state["resources"]["request"] == {"max_cores": 1, "max_memory_gb": 2}
    if scratch_root_text:
        process_dir = observed_process["cwd"]
        scratch_root = Path(scratch_root_text).resolve()
        assert process_dir.parent == scratch_root
        assert Path(observed_process["command"][1]).parent == process_dir
        assert Path(observed_process["command"][3]).parent == process_dir
        assert not process_dir.exists()
        provenance = state["engine_payload"]["scratch_provenance"]
        assert provenance["publication_status"] == "committed"
        assert set(provenance["published_files"]) == {
            "xtb.stdout.log",
            "xtb.stderr.log",
            "xtb.trj",
            "mdrestart",
            "xtbmdok",
        }
        assert {"charges", "wbo", "xtbtopo.mol"}.issubset(provenance["omitted_transient_files"])
        execution_dir = Path(completed.metadata["execution_dir"])
        assert not any(
            (execution_dir / name).exists() for name in ("charges", "wbo", "xtbtopo.mol")
        )
        assert not any(path.name.startswith("attempt-") for path in scratch_root.iterdir())
