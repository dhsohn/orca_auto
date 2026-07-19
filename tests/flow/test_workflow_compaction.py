from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import reserve_slot
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.store import QUEUE_FILE_NAME, save_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.flow.workflow import compaction


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _entry(*, engine: str, job_dir: Path, suffix: str) -> QueueEntry:
    return QueueEntry(
        queue_id=f"q-{engine}-{suffix}",
        app_name=f"orca_auto_{engine}",
        task_id=f"{engine}-job-{suffix}",
        task_kind="crest" if engine == "crest" else "xtb_path",
        engine=engine,
        status=QueueStatus.COMPLETED,
        enqueued_at="2026-07-20T00:00:00+00:00",
        started_at="2026-07-20T00:01:00+00:00",
        finished_at="2026-07-20T00:02:00+00:00",
        metadata={"job_dir": str(job_dir)},
    )


def _write_completed_state(job_dir: Path, entry: QueueEntry) -> None:
    _write_json(
        job_dir / "job_state.json",
        {
            "schema_version": 1,
            "engine": entry.engine,
            "job": {
                "id": entry.task_id,
                "task_id": entry.task_id,
                "queue_id": entry.queue_id,
                "app_name": entry.app_name,
                "dir": str(job_dir),
                "generation": queue_entry_generation_token(entry),
            },
            "status": {"state": "completed", "reason": "completed", "exit_code": 0},
            "recovery": {"pending": False},
            "process": {"worker_pid": None},
        },
    )


def _stage(*, engine: str, stage_id: str, job_dir: Path, entry: QueueEntry) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "stage_kind": "crest" if engine == "crest" else "xtb_path",
        "status": "completed",
        "metadata": {
            "queue_id": entry.queue_id,
            "child_job_id": entry.task_id,
        },
        "task": {
            "task_id": f"{stage_id}-task",
            "engine": engine,
            "task_kind": "crest" if engine == "crest" else "xtb_path",
            "status": "completed",
            "metadata": {},
            "payload": {"job_dir": str(job_dir)},
            "enqueue_payload": {"job_dir": str(job_dir)},
            "submission_result": {
                "queue_id": entry.queue_id,
                "job_dir": str(job_dir),
            },
        },
    }


def _workflow_payload(workspace: Path, stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "workflow_id": workspace.name,
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-07-20T00:00:00+00:00",
        "metadata": {
            "final_child_sync_pending": False,
            "final_child_sync_completed_at": "2026-07-20T00:03:00+00:00",
            "si_publish_pending": False,
            "si_publish_generation": "generation-1",
            "si_published_generation": "generation-1",
        },
        "stages": stages,
    }


def _write_registry(root: Path, workspace: Path) -> None:
    _write_json(
        root / "workflow_registry.json",
        [
            {
                "workflow_id": workspace.name,
                "status": "completed",
                "workspace_dir": str(workspace),
                "workflow_file": str(workspace / "workflow.json"),
                "metadata": {
                    "final_child_sync_pending": False,
                    "si_publish_pending": False,
                },
            }
        ],
    )


def _completed_workflow(
    tmp_path: Path,
    *,
    engine: str = "crest",
) -> tuple[Path, Path, Path, Path, QueueEntry]:
    root = tmp_path / "runs"
    workspace = root / f"wf-{engine}-compact"
    stage_id = f"{engine}_stage"
    queue_root = workspace / ("01_crest" if engine == "crest" else "02_xtb")
    job_dir = queue_root / stage_id / "job"
    job_dir.mkdir(parents=True)

    entry = _entry(engine=engine, job_dir=job_dir, suffix="1")
    save_entries(queue_root, [entry])
    _write_completed_state(job_dir, entry)
    _write_json(job_dir / "job_report.json", {"obsolete": True})
    (job_dir / "job_report.md").write_text("# obsolete\n", encoding="utf-8")
    (job_dir / "scientific-output.xyz").write_text("1\nkept\nH 0 0 0\n", encoding="utf-8")

    _write_json(
        workspace / "workflow.json",
        _workflow_payload(
            workspace,
            [_stage(engine=engine, stage_id=stage_id, job_dir=job_dir, entry=entry)],
        ),
    )
    _write_registry(root, workspace)
    return root, workspace, queue_root, job_dir, entry


def _completed_xtb_attempt_workflow(
    tmp_path: Path,
) -> tuple[Path, Path, Path, tuple[Path, Path]]:
    root = tmp_path / "runs"
    workspace = root / "wf-xtb-attempts"
    queue_root = workspace / "02_xtb"
    stage_id = "xtb_stage"
    job_dirs = (queue_root / stage_id / "attempt_00", queue_root / stage_id / "attempt_01")
    entries: list[QueueEntry] = []
    attempts: list[dict[str, Any]] = []
    for number, job_dir in enumerate(job_dirs):
        job_dir.mkdir(parents=True)
        entry = _entry(engine="xtb", job_dir=job_dir, suffix=str(number))
        entries.append(entry)
        attempts.append(
            {
                "attempt_number": number,
                "job_dir": str(job_dir),
                "queue_id": entry.queue_id,
                "job_id": entry.task_id,
            }
        )
        _write_completed_state(job_dir, entry)
        _write_json(job_dir / "job_report.json", {"attempt": number})
    save_entries(queue_root, entries)

    current = entries[1]
    current_dir = job_dirs[1]
    stage = _stage(engine="xtb", stage_id=stage_id, job_dir=current_dir, entry=current)
    stage["metadata"].update(
        {
            "xtb_active_attempt_number": 1,
            "xtb_attempts": attempts,
        }
    )
    stage["task"]["payload"]["xtb_active_attempt_number"] = 1
    _write_json(workspace / "workflow.json", _workflow_payload(workspace, [stage]))
    _write_registry(root, workspace)
    return root, workspace, queue_root, job_dirs


def test_dry_run_is_non_mutating_and_creates_no_receipt_state(tmp_path: Path) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)

    result = compaction.compact_completed_workflow(root, workspace)

    assert result.eligible is True
    assert result.blocked is False
    assert result.applied is False
    assert [item.relative_path for item in result.would_remove] == [
        "01_crest/crest_stage/job/job_report.json",
        "01_crest/crest_stage/job/job_report.md",
    ]
    assert (job_dir / "job_report.json").exists()
    assert (job_dir / "job_report.md").exists()
    assert not (root / ".orca_auto_compaction_receipts").exists()


@pytest.mark.parametrize("engine", ["crest", "xtb"])
def test_apply_removes_only_internal_report_copies(tmp_path: Path, engine: str) -> None:
    root, workspace, queue_root, job_dir, _entry_row = _completed_workflow(tmp_path, engine=engine)
    preserved = [
        workspace / "workflow.json",
        workspace / "workflow_report.html",
        workspace / "workflow_si.md",
        workspace / "si_data.csv",
        queue_root / "queue.lock",
        queue_root / "job_locations.lock",
        job_dir / "job_state.json",
        job_dir / "scientific-output.xyz",
        job_dir / "run.lock",
        workspace / "03_orca" / "orca-job" / "job_report.json",
        root / "standalone-xtb-md" / "job_report.md",
    ]
    for path in preserved:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preserve\n", encoding="utf-8")

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.eligible is True
    assert result.applied is True
    assert {Path(path).name for path in result.removed} == {
        "job_report.json",
        "job_report.md",
    }
    assert not (job_dir / "job_report.json").exists()
    assert not (job_dir / "job_report.md").exists()
    assert all(path.exists() for path in preserved)
    assert not (root / ".orca_auto_compaction_receipts").exists()


def test_rerun_finishes_after_interruption_between_unlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    real_unlink = compaction._unlink_candidate
    unlink_count = 0

    def crash_after_first(
        target_workspace: Path,
        candidate: compaction._Candidate,
    ) -> None:
        nonlocal unlink_count
        real_unlink(target_workspace, candidate)
        unlink_count += 1
        if unlink_count == 1:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(compaction, "_unlink_candidate", crash_after_first)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        compaction.compact_completed_workflow(root, workspace, apply=True)

    assert not (job_dir / "job_report.json").exists()
    assert (job_dir / "job_report.md").exists()
    monkeypatch.setattr(compaction, "_unlink_candidate", real_unlink)

    rerun = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert rerun.applied is True
    assert rerun.removed == ("01_crest/crest_stage/job/job_report.md",)
    assert not (job_dir / "job_report.md").exists()
    assert not (root / ".orca_auto_compaction_receipts").exists()


@pytest.mark.parametrize("replacement", ["hardlink", "symlink"])
def test_non_single_link_report_candidate_blocks_without_mutation(
    tmp_path: Path,
    replacement: str,
) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    report = job_dir / "job_report.json"
    target = job_dir / "report-target"
    report.rename(target)
    if replacement == "hardlink":
        report.hardlink_to(target)
    else:
        report.symlink_to(target.name)

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert report.exists()
    assert (job_dir / "job_report.md").exists()
    assert not (root / ".orca_auto_compaction_receipts").exists()


def test_symlinked_queue_state_blocks(tmp_path: Path) -> None:
    root, workspace, queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    queue_file = queue_root / QUEUE_FILE_NAME
    target = queue_root / "queue-target.json"
    queue_file.rename(target)
    queue_file.symlink_to(target.name)

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert (job_dir / "job_report.json").exists()


def test_matching_default_admission_slot_blocks(tmp_path: Path) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    token = reserve_slot(
        root / ".admission",
        1,
        source="test",
        workflow_id=workspace.name,
    )
    assert token is not None

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert "admission" in result.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_admission_work_dir_anywhere_in_workflow_blocks(tmp_path: Path) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    active_orca_dir = workspace / "03_orca" / "orca-stage" / "job"
    token = reserve_slot(
        root / ".admission",
        1,
        source="test",
        app_name="orca_auto_orca",
        work_dir=active_orca_dir,
    )
    assert token is not None

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert "admission" in result.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_other_terminal_generation_sharing_job_dir_blocks(tmp_path: Path) -> None:
    root, workspace, queue_root, job_dir, entry = _completed_workflow(tmp_path)
    other = _entry(engine="crest", job_dir=job_dir, suffix="other")
    save_entries(queue_root, [entry, other])

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert "another queue generation" in result.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_xtb_current_and_history_must_match_exactly(tmp_path: Path) -> None:
    root, workspace, _queue_root, job_dirs = _completed_xtb_attempt_workflow(tmp_path)

    valid = compaction.compact_completed_workflow(root, workspace)

    assert valid.eligible is True
    assert [artifact.job_id for artifact in valid.would_remove] == [
        "xtb-job-0",
        "xtb-job-1",
    ]

    workflow_path = workspace / "workflow.json"
    payload = json.loads(workflow_path.read_text(encoding="utf-8"))
    payload["stages"][0]["metadata"]["xtb_attempts"][1]["job_id"] = "wrong-current-job"
    _write_json(workflow_path, payload)

    mismatched = compaction.compact_completed_workflow(root, workspace)

    assert mismatched.blocked is True
    assert "active attempt identity mismatch" in mismatched.reasons[0]
    assert all(job_dir.joinpath("job_report.json").exists() for job_dir in job_dirs)


def test_terminal_failed_non_internal_stage_does_not_block(tmp_path: Path) -> None:
    root, workspace, _queue_root, _job_dir, _entry_row = _completed_workflow(tmp_path)
    workflow_path = workspace / "workflow.json"
    payload = json.loads(workflow_path.read_text(encoding="utf-8"))
    payload["stages"].append(
        {
            "stage_id": "optional_orca_candidate",
            "status": "failed",
            "task": {"engine": "orca", "status": "failed"},
        }
    )
    _write_json(workflow_path, payload)

    result = compaction.compact_completed_workflow(root, workspace)

    assert result.eligible is True


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("final_child_sync_completed_at", "", "final child synchronization"),
        ("si_published_generation", "another-generation", "SI publication"),
        ("si_publish_generation", "", "SI publication"),
    ],
)
def test_incomplete_finalization_blocks(
    tmp_path: Path,
    field: str,
    value: str,
    reason: str,
) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    workflow_path = workspace / "workflow.json"
    payload = json.loads(workflow_path.read_text(encoding="utf-8"))
    payload["metadata"][field] = value
    _write_json(workflow_path, payload)

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert reason in result.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_registry_identity_must_be_exact(tmp_path: Path) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    registry_path = root / "workflow_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry[0]["status"] = "failed"
    _write_json(registry_path, registry)

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert "exact completed registry" in result.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_exact_index_is_used_only_when_queue_row_is_absent(tmp_path: Path) -> None:
    root, workspace, queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    save_entries(queue_root, [])
    index_path = queue_root / "job_locations.json"
    index_row = {
        "job_id": "crest-job-1",
        "app_name": "orca_auto_crest",
        "status": "completed",
        "original_run_dir": str(job_dir),
        "latest_known_path": str(job_dir),
    }
    _write_json(index_path, [index_row])

    exact = compaction.compact_completed_workflow(root, workspace)
    assert exact.eligible is True

    index_row["latest_known_path"] = str(job_dir.parent / "other")
    _write_json(index_path, [index_row])
    mismatched = compaction.compact_completed_workflow(root, workspace)

    assert mismatched.blocked is True
    assert "indexed generation" in mismatched.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_index_fallback_rejects_other_generation_for_same_job_dir(tmp_path: Path) -> None:
    root, workspace, queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    save_entries(queue_root, [])
    exact = {
        "job_id": "crest-job-1",
        "app_name": "orca_auto_crest",
        "status": "completed",
        "original_run_dir": str(job_dir),
        "latest_known_path": str(job_dir),
    }
    _write_json(
        queue_root / "job_locations.json",
        [exact, {**exact, "job_id": "crest-job-other"}],
    )

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert "another indexed generation" in result.reasons[0]
    assert (job_dir / "job_report.json").exists()


def test_candidate_budget_bounds_block_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, _queue_root, job_dir, _entry_row = _completed_workflow(tmp_path)
    monkeypatch.setattr(compaction, "_MAX_CANDIDATES", 1)

    result = compaction.compact_completed_workflow(root, workspace, apply=True)

    assert result.blocked is True
    assert "candidate count" in result.reasons[0]
    assert (job_dir / "job_report.json").exists()
    assert (job_dir / "job_report.md").exists()
