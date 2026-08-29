from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto.core.utils import parse_iso_utc
from orca_auto.orca.run_snapshot import collect_run_snapshots
from orca_auto.orca.state import save_state
from orca_auto.orca.state_reading import state_path


def test_parse_iso_utc_handles_invalid_z_naive_and_offset_values() -> None:
    assert parse_iso_utc(None) is None
    assert parse_iso_utc("not-a-timestamp") is None

    parsed_z = parse_iso_utc("2026-01-10T12:00:00Z")
    assert parsed_z == datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    parsed_naive = parse_iso_utc("2026-01-10T12:00:00")
    assert parsed_naive == datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    parsed_offset = parse_iso_utc("2026-01-10T21:00:00+09:00")
    assert parsed_offset == datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)


def test_collect_run_snapshots_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert collect_run_snapshots(tmp_path / "missing") == []


def test_collect_run_snapshots_skips_state_files_that_fail_to_load(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn"
    reaction_dir.mkdir(parents=True)
    state_path(reaction_dir).write_text("{}", encoding="utf-8")

    assert collect_run_snapshots(allowed_root) == []


def test_collect_run_snapshots_skips_workflow_workspace_jobs(tmp_path: Path) -> None:
    allowed_root = tmp_path / "runs"
    standalone = allowed_root / "rxn_standalone"
    standalone.mkdir(parents=True)
    save_state(
        standalone,
        {
            "run_id": "run-standalone",
            "status": "completed",
            "started_at": "2026-01-10T10:00:00+00:00",
            "updated_at": "2026-01-10T11:00:00+00:00",
            "selected_inp": str(standalone / "calc.inp"),
            "attempts": [],
            "final_result": {"completed_at": "2026-01-10T11:00:00+00:00"},
        },
    )

    workspace = allowed_root / "wf_20260704"
    stage_job = workspace / "03_orca" / "candidate_01"
    stage_job.mkdir(parents=True)
    (workspace / "workflow.json").write_text("{}", encoding="utf-8")
    save_state(
        stage_job,
        {
            "run_id": "run-workflow-internal",
            "status": "completed",
            "started_at": "2026-01-10T10:00:00+00:00",
            "updated_at": "2026-01-10T11:00:00+00:00",
            "selected_inp": str(stage_job / "calc.inp"),
            "attempts": [],
            "final_result": {"completed_at": "2026-01-10T11:00:00+00:00"},
        },
    )

    snapshots = collect_run_snapshots(allowed_root)

    assert [snapshot.run_id for snapshot in snapshots] == ["run-standalone"]


def test_collect_run_snapshots_skips_state_symlink_that_escapes_runs_root(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    linked_job = allowed_root / "linked-job"
    outside_job = tmp_path / "outside-job"
    linked_job.mkdir(parents=True)
    outside_job.mkdir()
    save_state(
        outside_job,
        {
            "run_id": "run-outside",
            "status": "completed",
            "started_at": "2026-01-10T10:00:00+00:00",
            "updated_at": "2026-01-10T11:00:00+00:00",
            "selected_inp": str(outside_job / "calc.inp"),
            "attempts": [],
            "final_result": {"completed_at": "2026-01-10T11:00:00+00:00"},
        },
    )
    state_path(linked_job).symlink_to(state_path(outside_job))
    (allowed_root / "job_locations.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "job-linked",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(linked_job),
                    "molecule_key": "linked-job",
                    "selected_input_xyz": str(linked_job / "calc.inp"),
                    "latest_known_path": str(linked_job),
                    "resource_request": {},
                    "resource_actual": {},
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    assert collect_run_snapshots(allowed_root) == []


def test_collect_run_snapshots_builds_basic_snapshot_fields(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "group" / "rxn"
    reaction_dir.mkdir(parents=True)
    out_path = reaction_dir / "calc.out"
    out_path.write_text("done", encoding="utf-8")

    state = {
        "run_id": "run-123",
        "status": "RUNNING",
        "started_at": "2026-01-10T10:00:00+00:00",
        "updated_at": "2026-01-10T11:01:01+00:00",
        "selected_inp": str(reaction_dir / "calc.inp"),
        "attempts": [{"out_path": str(out_path)}, {"out_path": str(out_path)}],
        "final_result": {
            "completed_at": "2026-01-10T11:01:01+00:00",
            "reason": "  terminated_normally  ",
        },
    }
    save_state(reaction_dir, state)
    persisted_state = json.loads(state_path(reaction_dir).read_text(encoding="utf-8"))
    persisted_state["timestamps"]["updated_at"] = "2026-01-10T11:01:01+00:00"
    state_path(reaction_dir).write_text(
        json.dumps(persisted_state, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    snapshots = collect_run_snapshots(allowed_root)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.key == "run-123"
    assert snapshot.name == "group/rxn"
    assert snapshot.reaction_dir == reaction_dir
    assert snapshot.run_id == "run-123"
    assert snapshot.status == "running"
    assert snapshot.started_at == "2026-01-10T10:00:00+00:00"
    assert snapshot.updated_at == "2026-01-10T11:01:01+00:00"
    assert snapshot.completed_at == "2026-01-10T11:01:01+00:00"
    assert snapshot.selected_inp_name == "calc.inp"
    assert snapshot.attempts == 2


def test_collect_run_snapshots_uses_tracking_record_for_tracked_run(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    organized_root = tmp_path / "organized"
    allowed_root.mkdir()
    tracked_run = organized_root / "project" / "rxn_tracked"
    tracked_run.mkdir(parents=True)

    out_path = tracked_run / "calc.out"
    out_path.write_text("done", encoding="utf-8")
    state = {
        "run_id": "run-tracked",
        "status": "completed",
        "started_at": "2026-01-10T10:00:00+00:00",
        "updated_at": "2026-01-10T11:01:01+00:00",
        "selected_inp": str(tracked_run / "calc.inp"),
        "attempts": [{"out_path": str(out_path)}],
        "final_result": {
            "completed_at": "2026-01-10T11:01:01+00:00",
            "reason": "tracked_completion",
            "last_out_path": str(out_path),
        },
    }
    save_state(tracked_run, state)

    original_run = allowed_root / "project" / "rxn_tracked"
    job_locations = [
        {
            "job_id": "job-tracked",
            "app_name": "orca_auto_orca",
            "job_type": "orca_opt",
            "status": "completed",
            "original_run_dir": str(original_run),
            "molecule_key": "rxn_tracked",
            "selected_input_xyz": str(tracked_run / "calc.inp"),
            "latest_known_path": str(tracked_run),
            "resource_request": {"max_cores": 8},
            "resource_actual": {"max_cores": 8},
        }
    ]
    (allowed_root / "job_locations.json").write_text(
        json.dumps(job_locations, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    snapshots = collect_run_snapshots(allowed_root)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.name == "project/rxn_tracked"
    assert snapshot.reaction_dir == tracked_run.resolve()
    assert snapshot.run_id == "run-tracked"
    assert snapshot.selected_inp_name == "calc.inp"


def test_collect_run_snapshots_includes_untracked_state_when_index_is_incomplete(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    organized_root = tmp_path / "organized"
    allowed_root.mkdir()

    tracked_run = organized_root / "project" / "rxn_tracked"
    tracked_run.mkdir(parents=True)
    tracked_state = {
        "run_id": "run-tracked",
        "status": "completed",
        "started_at": "2026-01-10T10:00:00+00:00",
        "updated_at": "2026-01-10T11:01:01+00:00",
        "selected_inp": str(tracked_run / "calc.inp"),
        "attempts": [],
        "final_result": {
            "completed_at": "2026-01-10T11:01:01+00:00",
            "reason": "tracked_completion",
        },
    }
    save_state(tracked_run, tracked_state)

    untracked_run = allowed_root / "untracked" / "rxn_untracked"
    untracked_run.mkdir(parents=True)
    untracked_state: dict[str, Any] = {
        "run_id": "run-untracked",
        "status": "running",
        "started_at": "2026-01-10T09:00:00+00:00",
        "updated_at": "2026-01-10T10:00:00+00:00",
        "selected_inp": str(untracked_run / "untracked.inp"),
        "attempts": [],
        "final_result": None,
    }
    save_state(untracked_run, untracked_state)

    job_locations = [
        {
            "job_id": "job-tracked",
            "app_name": "orca_auto_orca",
            "job_type": "orca_opt",
            "status": "completed",
            "original_run_dir": str(allowed_root / "project" / "rxn_tracked"),
            "molecule_key": "rxn_tracked",
            "selected_input_xyz": str(tracked_run / "calc.inp"),
            "latest_known_path": str(tracked_run),
            "resource_request": {},
            "resource_actual": {},
        }
    ]
    (allowed_root / "job_locations.json").write_text(
        json.dumps(job_locations, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    snapshots = collect_run_snapshots(allowed_root)

    assert {snapshot.run_id for snapshot in snapshots} == {"run-tracked", "run-untracked"}
    assert {snapshot.name for snapshot in snapshots} == {
        "project/rxn_tracked",
        "untracked/rxn_untracked",
    }
