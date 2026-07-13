from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.indexing import get_job_location
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    list_queue,
    mark_completed,
)
from orca_auto.flow.engines.crest import queue_runtime as queue_cmd
from orca_auto.flow.engines.crest import submission as crest_submission
from orca_auto.flow.engines.crest.runner import CrestRunResult
from orca_auto.flow.engines.crest.state import load_report_json, load_state
from orca_auto.flow.submitters import crest as crest_submitter
from tests.engine_artifact_helpers import (
    engine_payload as _engine_payload,
)
from tests.engine_artifact_helpers import (
    input_payload as _input_payload,
)
from tests.engine_artifact_helpers import (
    job as _job,
)
from tests.engine_artifact_helpers import (
    resources as _resources,
)
from tests.engine_artifact_helpers import (
    status as _status,
)
from tests.engine_process_helpers import process_one_crest_for_test


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    workflow_root = tmp_path / "workflow_root"
    allowed_root = workflow_root / "wf_001" / "01_crest"
    allowed_root.mkdir(parents=True)
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    crest_executable = executable_dir / "crest"
    xtb_executable = executable_dir / "xtb"
    for executable in (crest_executable, xtb_executable):
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {json.dumps(str(workflow_root))}",
                "workflow:",
                "  paths:",
                f"    crest_executable: {json.dumps(str(crest_executable))}",
                f"    xtb_executable: {json.dumps(str(xtb_executable))}",
                "resources:",
                "  max_cores_per_task: 6",
                "  max_memory_gb_per_task: 14",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path, allowed_root


def _write_xyz(path: Path, label: str = "sample") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"2\n{label}\nH 0.0 0.0 0.0\nH 0.0 0.0 0.7\n",
        encoding="utf-8",
    )


def _patch_crest_e2e_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    queued_notifications: list[dict[str, Any]] = []
    started_notifications: list[dict[str, Any]] = []
    finished_notifications: list[dict[str, Any]] = []

    def fake_notify_job_queued(cfg: Any, **kwargs: Any) -> bool:
        queued_notifications.append(kwargs)
        return True

    def fake_notify_job_started(cfg: Any, **kwargs: Any) -> bool:
        started_notifications.append(kwargs)
        return True

    def fake_notify_job_finished(cfg: Any, **kwargs: Any) -> bool:
        finished_notifications.append(kwargs)
        return True

    monkeypatch.setattr(crest_submission, "notify_job_queued", fake_notify_job_queued)
    monkeypatch.setattr(queue_cmd, "notify_job_started", fake_notify_job_started)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", fake_notify_job_finished)
    return queued_notifications, started_notifications, finished_notifications


def _patch_crest_e2e_runner(monkeypatch: pytest.MonkeyPatch, job_dir: Path) -> None:
    class _FakeProcess:
        def poll(self) -> int | None:
            return 0

    def fake_start_crest_job(
        cfg: Any,
        *,
        job_dir: Path,
        selected_xyz: Path,
        execution_snapshot: dict[str, Any],
    ) -> Any:
        return type(
            "Running",
            (),
            {"process": _FakeProcess(), "selected_input_xyz": str(selected_xyz.resolve())},
        )()

    def fake_finalize_crest_job(running: Any) -> CrestRunResult:
        selected_xyz = Path(running.selected_input_xyz)
        stdout_log = job_dir / "crest.stdout.log"
        stderr_log = job_dir / "crest.stderr.log"
        retained_path = job_dir / "crest_best.xyz"
        stdout_log.write_text("stdout\n", encoding="utf-8")
        stderr_log.write_text("stderr\n", encoding="utf-8")
        retained_path.write_text("1\nretained\nH 0.0 0.0 0.0\n", encoding="utf-8")
        return CrestRunResult(
            status="completed",
            reason="ok",
            command=("crest", selected_xyz.name, "--T", "6"),
            exit_code=0,
            started_at="2026-04-20T00:00:00+00:00",
            finished_at="2026-04-20T00:05:00+00:00",
            stdout_log=str(stdout_log.resolve()),
            stderr_log=str(stderr_log.resolve()),
            selected_input_xyz=str(selected_xyz.resolve()),
            mode="standard",
            retained_conformer_count=1,
            retained_conformer_paths=(str(retained_path.resolve()),),
            manifest_path=str((job_dir / "crest_job.yaml").resolve()),
            resource_request={"max_cores": 6, "max_memory_gb": 14},
            resource_actual={"assigned_cores": 6, "memory_limit_gb": 14},
        )

    monkeypatch.setattr(queue_cmd, "start_crest_job", fake_start_crest_job)
    monkeypatch.setattr(queue_cmd, "finalize_crest_job", fake_finalize_crest_job)


def _prepare_crest_e2e_job(job_dir: Path) -> None:
    job_dir.mkdir(parents=True)
    _write_xyz(job_dir / "input.xyz", "input")
    (job_dir / "crest_job.yaml").write_text(
        "mode: standard\ninput_xyz: input.xyz\n", encoding="utf-8"
    )


def test_cmd_run_dir_queues_job_updates_state_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, allowed_root = _write_config(tmp_path)
    job_dir = allowed_root / "job-queue"
    job_dir.mkdir(parents=True)
    _write_xyz(job_dir / "fallback.xyz", "fallback")
    _write_xyz(job_dir / "preferred.xyz", "preferred")
    (job_dir / "crest_job.yaml").write_text(
        "mode: nci\ninput_xyz: preferred.xyz\nresources:\n  max_cores: 9\n  max_memory_gb: 21\n",
        encoding="utf-8",
    )

    notifications: list[dict[str, Any]] = []
    monkeypatch.setattr(crest_submission, "new_job_id", lambda: "crest-fixed-id")

    def fake_notify_job_queued(cfg: Any, **kwargs: Any) -> bool:
        notifications.append(kwargs)
        return True

    monkeypatch.setattr(crest_submission, "notify_job_queued", fake_notify_job_queued)

    submission = crest_submitter.submit_job_dir(
        job_dir=str(job_dir),
        priority=4,
        config_path=str(config_path),
    )

    capsys.readouterr()
    queue_entries = list_queue(allowed_root)
    state = load_state(job_dir)
    record = get_job_location(allowed_root, "crest-fixed-id")

    assert submission["status"] == "submitted"
    assert submission["job_id"] == "crest-fixed-id"
    assert submission["parsed_stdout"]["status"] == "queued"
    assert submission["parsed_stdout"]["priority"] == "4"

    assert len(queue_entries) == 1
    entry = queue_entries[0]
    selected_snapshot = Path(entry.metadata["selected_input_xyz"])
    assert selected_snapshot.is_relative_to(job_dir / ".orca_auto_input_snapshots")
    assert selected_snapshot.read_text(encoding="utf-8") == (job_dir / "preferred.xyz").read_text(
        encoding="utf-8"
    )
    assert entry.task_id == "crest-fixed-id"
    assert entry.priority == 4
    expected_metadata = {
        "job_dir": str(job_dir.resolve()),
        "selected_input_xyz": str(selected_snapshot),
        "mode": "nci",
        "molecule_key": "preferred",
        "manifest_present": "true",
        "resource_request": {"max_cores": 9, "max_memory_gb": 21},
        "resource_actual": {"max_cores": 9, "max_memory_gb": 21},
        QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_COMPLETE,
    }
    assert {key: entry.metadata[key] for key in expected_metadata} == expected_metadata
    assert entry.metadata[QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0
    assert entry.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY]
    assert entry.metadata[QUEUE_RECORD_SYNC_UPDATED_AT_KEY]

    assert state is not None
    assert _job(state)["id"] == "crest-fixed-id"
    assert _job(state)["dir"] == str(job_dir.resolve())
    assert _input_payload(state)["selected_xyz_path"] == str(selected_snapshot)
    assert _status(state)["state"] == "queued"
    assert _engine_payload(state)["mode"] == "nci"
    assert _engine_payload(state)["molecule_key"] == "preferred"
    assert _resources(state)["request"] == {"max_cores": 9, "max_memory_gb": 21}
    assert _resources(state)["actual"] == {"max_cores": 9, "max_memory_gb": 21}

    assert record is not None
    assert record.job_id == "crest-fixed-id"
    assert record.status == "queued"
    assert record.job_type == "crest_nci_conformer_search"
    assert record.original_run_dir == str(job_dir.resolve())
    assert record.latest_known_path == str(job_dir.resolve())
    assert record.selected_input_xyz == str(selected_snapshot)
    assert record.resource_request == {"max_cores": 9, "max_memory_gb": 21}
    assert record.resource_actual == {"max_cores": 9, "max_memory_gb": 21}

    assert notifications == [
        {
            "job_id": "crest-fixed-id",
            "queue_id": entry.queue_id,
            "job_dir": job_dir.resolve(),
            "mode": "nci",
            "selected_xyz": selected_snapshot,
        }
    ]


def test_cmd_run_dir_replays_active_job_dir_and_allows_terminal_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, allowed_root = _write_config(tmp_path)
    job_dir = allowed_root / "job-duplicate"
    job_dir.mkdir(parents=True)
    _write_xyz(job_dir / "input.xyz", "input")

    notifications: list[dict[str, Any]] = []
    job_ids = iter(("crest-first-id", "crest-second-id", "crest-third-id"))
    monkeypatch.setattr(crest_submission, "new_job_id", lambda: next(job_ids))

    def fake_notify_job_queued(cfg: Any, **kwargs: Any) -> bool:
        notifications.append(kwargs)
        return True

    monkeypatch.setattr(crest_submission, "notify_job_queued", fake_notify_job_queued)

    first_submission = crest_submitter.submit_job_dir(
        job_dir=str(job_dir),
        priority=10,
        config_path=str(config_path),
    )
    capsys.readouterr()

    second_submission = crest_submitter.submit_job_dir(
        job_dir=str(job_dir),
        priority=10,
        config_path=str(config_path),
    )
    capsys.readouterr()

    queue_entries = list_queue(allowed_root)
    state = load_state(job_dir)

    assert first_submission["status"] == "submitted"
    assert second_submission["status"] == "submitted"
    assert second_submission["queue_id"] == first_submission["queue_id"]
    assert second_submission["job_id"] == "crest-first-id"
    assert "reused existing queue entry" in second_submission["parsed_stdout"]["warning"]
    assert "task_id=crest-first-id" in second_submission["stderr"]

    assert len(queue_entries) == 1
    assert queue_entries[0].task_id == "crest-first-id"
    assert state is not None
    assert _job(state)["id"] == "crest-first-id"
    assert len(notifications) == 1

    completed = mark_completed(allowed_root, queue_entries[0].queue_id)
    assert completed is not None
    third_submission = crest_submitter.submit_job_dir(
        job_dir=str(job_dir),
        priority=10,
        config_path=str(config_path),
    )
    capsys.readouterr()

    assert third_submission["status"] == "submitted"
    assert third_submission["job_id"] == "crest-third-id"
    assert [entry.task_id for entry in list_queue(allowed_root)] == [
        "crest-first-id",
        "crest-third-id",
    ]
    assert len(notifications) == 2


def test_cli_end_to_end_smoke_path_submission_worker_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, allowed_root = _write_config(tmp_path)
    job_dir = allowed_root / "job-e2e"
    monkeypatch.setattr(crest_submission, "new_job_id", lambda: "crest-e2e-001")
    queued_notifications, started_notifications, finished_notifications = (
        _patch_crest_e2e_notifications(monkeypatch)
    )
    _patch_crest_e2e_runner(monkeypatch, job_dir)
    _prepare_crest_e2e_job(job_dir)

    submission = crest_submitter.submit_job_dir(
        job_dir=str(job_dir),
        priority=2,
        config_path=str(config_path),
    )
    capsys.readouterr()
    assert submission["status"] == "submitted"
    assert submission["job_id"] == "crest-e2e-001"

    assert (
        process_one_crest_for_test(
            queue_cmd,
            queue_cmd.load_config(str(config_path)),
        )
        == "processed"
    )
    worker_output = capsys.readouterr().out
    assert "organized_output_dir:" not in worker_output
    assert "queue_id:" in worker_output
    assert "job_id: crest-e2e-001" in worker_output
    assert "status: completed" in worker_output

    queue_entries = list_queue(allowed_root)
    assert len(queue_entries) == 1
    assert queue_entries[0].task_id == "crest-e2e-001"
    assert queue_entries[0].status.value == "completed"

    state = load_state(job_dir)
    report = load_report_json(job_dir)
    assert state is not None
    assert report is not None
    assert _status(state)["state"] == "completed"
    assert _status(report)["state"] == "completed"
    assert _engine_payload(report)["retained_conformer_count"] == 1

    record = get_job_location(allowed_root, "crest-e2e-001")
    assert record is not None
    assert record.original_run_dir == str(job_dir.resolve())
    assert record.latest_known_path == str(job_dir.resolve())

    assert len(queued_notifications) == 1
    assert queued_notifications[0]["job_id"] == "crest-e2e-001"
    assert len(started_notifications) == 1
    assert started_notifications[0]["job_id"] == "crest-e2e-001"
    assert started_notifications[0]["queue_id"].startswith("q_")
    assert Path(started_notifications[0]["job_dir"]).resolve() == job_dir.resolve()

    assert len(finished_notifications) == 1
    assert finished_notifications[0]["job_id"] == "crest-e2e-001"
    assert finished_notifications[0]["status"] == "completed"
