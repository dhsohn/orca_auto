from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow.engines.xtb import state as state_mod
from orca_auto.flow.engines.xtb import worker_terminal as worker_terminal
from orca_auto.flow.engines.xtb.runner import XtbRunResult


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(tmp_path)),
        resources=SimpleNamespace(max_cores_per_task=4, max_memory_gb_per_task=8),
    )


def _entry(job_dir: Path, selected_xyz: Path) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id="queue-1",
        app_name="orca_auto",
        task_id="job-1",
        started_at="2026-04-20T00:00:00Z",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "path_search",
            "reaction_key": "rxn-1",
            "input_summary": {"candidate_count": 1, "candidate_paths": [str(selected_xyz)]},
        },
    )


def _result(selected_xyz: Path, *, status: str = "completed") -> XtbRunResult:
    return XtbRunResult(
        status=status,
        reason="xtb_ok" if status == "completed" else "runner_error",
        command=("xtb", str(selected_xyz)),
        exit_code=0 if status == "completed" else 1,
        started_at="2026-04-20T00:00:00Z",
        finished_at="2026-04-20T00:05:00Z",
        stdout_log=str((selected_xyz.parent / "xtb.stdout.log").resolve()),
        stderr_log=str((selected_xyz.parent / "xtb.stderr.log").resolve()),
        selected_input_xyz=str(selected_xyz.resolve()),
        job_type="path_search",
        reaction_key="rxn-1",
        input_summary={"candidate_count": 1, "candidate_paths": [str(selected_xyz)]},
        candidate_count=1,
        selected_candidate_paths=(str(selected_xyz),),
        candidate_details=({"path": str(selected_xyz)},),
        analysis_summary={"candidate_paths": [str(selected_xyz)]},
        manifest_path=str((selected_xyz.parent / "xtb_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 8},
    )


def test_write_running_state_records_worker_job_pid(tmp_path: Path) -> None:
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz)

    worker_terminal.write_running_state(_cfg(tmp_path), entry, worker_job_pid=4242)

    payload = state_mod.load_state(job_dir)
    assert payload is not None
    assert payload["status"]["state"] == "running"
    assert payload["process"]["worker_pid"] == 4242
    assert payload["engine_payload"]["job_type"] == "path_search"


def test_write_execution_artifacts_writes_terminal_state_and_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz)
    result = _result(selected_xyz)

    worker_terminal.write_execution_artifacts(entry, result)

    state = state_mod.load_state(job_dir)
    report = state_mod.load_report_json(job_dir)
    assert state is not None
    assert report is not None
    assert state["status"]["state"] == "completed"
    assert report["status"]["reason"] == "xtb_ok"
    assert report["engine_payload"]["selected_candidate_paths"] == [str(selected_xyz)]


def test_finalize_execution_result_syncs_terminal_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz)
    result = _result(selected_xyz)
    completed_calls: list[tuple[Any, str, dict[str, Any] | None]] = []
    record_calls: list[dict[str, Any]] = []
    finished_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        worker_terminal,
        "mark_completed",
        lambda root, queue_id, metadata_update=None: completed_calls.append(
            (root, queue_id, metadata_update)
        ),
    )
    monkeypatch.setattr(
        worker_terminal,
        "mark_cancelled",
        lambda *args, **kwargs: pytest.fail("unexpected cancelled mark"),
    )
    monkeypatch.setattr(
        worker_terminal,
        "mark_failed",
        lambda *args, **kwargs: pytest.fail("unexpected failed mark"),
    )
    monkeypatch.setattr(
        worker_terminal,
        "upsert_job_record",
        lambda *args, **kwargs: record_calls.append(kwargs),
    )

    def fake_notify_finished(*args: Any, **kwargs: Any) -> bool:
        finished_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        worker_terminal,
        "notify_job_finished",
        fake_notify_finished,
    )

    outcome = worker_terminal.finalize_execution_result(
        _cfg(tmp_path),
        queue_root=queue_root,
        entry=entry,
        result=result,
        emit_output=False,
    )

    assert outcome == worker_terminal.WorkerExecutionOutcome(result=result, organized_output_dir="")
    assert completed_calls == [
        (str(queue_root), "queue-1", {"candidate_count": 1, "job_type": "path_search"})
    ]
    assert record_calls and record_calls[0]["job_id"] == "job-1"
    assert finished_calls and finished_calls[0]["status"] == "completed"
