from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orca_auto.orca import run_snapshot
from orca_auto.orca.run_snapshot import (
    RunSnapshot,
    _compute_elapsed,
    _find_latest_out_in_dir,
    _latest_out_path,
    collect_run_snapshots,
    elapsed_text,
    parse_iso_utc,
    sort_snapshots_by_completed,
    sort_snapshots_by_started,
)
from orca_auto.orca.state import save_state, state_path


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        current = cls(2026, 1, 10, 12, 0, 0, tzinfo=UTC)
        if tz is None:
            return current
        return current.astimezone(tz)


def _snapshot(
    reaction_dir: Path,
    *,
    name: str,
    started_at: str,
    updated_at: str = "",
    completed_at: str = "",
) -> RunSnapshot:
    return RunSnapshot(
        key=f"key-{name}",
        name=name,
        reaction_dir=reaction_dir,
        run_id=f"run-{name}",
        status="running",
        started_at=started_at,
        updated_at=updated_at,
        completed_at=completed_at,
        selected_inp_name="calc.inp",
        attempts=1,
        latest_out_path=None,
        final_reason="",
        elapsed=0.0,
        elapsed_text="0s",
    )


def test_find_latest_out_in_dir_handles_non_dir_stat_errors_and_latest_selection(
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "missing"
    assert _find_latest_out_in_dir(missing_dir) is None

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    skipped_dir = run_dir / "skip.out"
    skipped_dir.mkdir()
    bad_out = run_dir / "bad.out"
    bad_out.write_text("bad", encoding="utf-8")
    old_out = run_dir / "old.out"
    newest_out = run_dir / "new.out"
    old_out.write_text("old", encoding="utf-8")
    newest_out.write_text("new", encoding="utf-8")

    now = datetime.now(UTC).timestamp()
    os.utime(old_out, (now - 20, now - 20))
    os.utime(newest_out, (now - 5, now - 5))

    original_is_file = Path.is_file
    original_stat = Path.stat

    def _is_file(self: Path) -> bool:
        if self == bad_out:
            return True
        return original_is_file(self)

    def _stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == bad_out:
            raise OSError("boom")
        return original_stat(self, follow_symlinks=follow_symlinks)

    with (
        patch("pathlib.Path.is_file", autospec=True, side_effect=_is_file),
        patch("pathlib.Path.stat", autospec=True, side_effect=_stat),
    ):
        assert _find_latest_out_in_dir(run_dir) == newest_out


def test_parse_iso_utc_handles_invalid_z_naive_and_offset_values() -> None:
    assert parse_iso_utc(None) is None
    assert parse_iso_utc("not-a-timestamp") is None

    parsed_z = parse_iso_utc("2026-01-10T12:00:00Z")
    assert parsed_z == datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    parsed_naive = parse_iso_utc("2026-01-10T12:00:00")
    assert parsed_naive == datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    parsed_offset = parse_iso_utc("2026-01-10T21:00:00+09:00")
    assert parsed_offset == datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)


def test_elapsed_text_formats_negative_hour_minute_and_second_ranges() -> None:
    assert elapsed_text(-1.0) == "-"
    assert elapsed_text(3661.0) == "1h 01m"
    assert elapsed_text(125.0) == "2m 05s"
    assert elapsed_text(9.0) == "9s"


def test_compute_elapsed_handles_missing_started_terminal_and_running(monkeypatch) -> None:
    assert _compute_elapsed({"status": "running"}) == -1.0

    completed_elapsed = _compute_elapsed(
        {
            "status": "completed",
            "started_at": "2026-01-10T10:00:00+00:00",
            "updated_at": "2026-01-10T11:30:00+00:00",
        }
    )
    assert completed_elapsed == 5400.0

    monkeypatch.setattr(run_snapshot, "datetime", _FrozenDateTime)
    running_elapsed = _compute_elapsed(
        {
            "status": "running",
            "started_at": "2026-01-10T11:45:00+00:00",
        }
    )
    assert running_elapsed == 900.0


def test_latest_out_path_prefers_final_result_when_resolvable(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    final_out = reaction_dir / "final.out"
    final_out.write_text("done", encoding="utf-8")
    attempt_out = reaction_dir / "attempt.out"
    attempt_out.write_text("attempt", encoding="utf-8")

    resolved = _latest_out_path(
        reaction_dir,
        {
            "final_result": {"last_out_path": "final.out"},
            "attempts": [{"out_path": "attempt.out"}],
        },
    )

    assert resolved == final_out.resolve()


def test_latest_out_path_uses_latest_valid_attempt_after_skipping_invalid_entries(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    attempt_out = reaction_dir / "attempt.out"
    attempt_out.write_text("attempt", encoding="utf-8")

    resolved = _latest_out_path(
        reaction_dir,
        {
            "final_result": {"last_out_path": "missing.out"},
            "attempts": [
                "invalid",
                {"out_path": ""},
                {"other": "value"},
                {"out_path": "attempt.out"},
            ],
        },
    )

    assert resolved == attempt_out.resolve()


def test_latest_out_path_falls_back_to_latest_output_in_directory(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    older = reaction_dir / "older.out"
    newer = reaction_dir / "newer.out"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    older.touch()
    newer.touch()
    older_mtime = datetime(2026, 1, 10, 11, 0, 0, tzinfo=UTC).timestamp()
    newer_mtime = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC).timestamp()
    older.touch()
    newer.touch()
    import os

    os.utime(older, (older_mtime, older_mtime))
    os.utime(newer, (newer_mtime, newer_mtime))

    resolved = _latest_out_path(
        reaction_dir,
        {"final_result": None, "attempts": [{"out_path": "missing.out"}, 123]},
    )

    assert resolved == newer


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


def test_collect_run_snapshots_builds_basic_snapshot_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
    monkeypatch.setattr(run_snapshot, "_compute_elapsed", lambda _state: 3661.0)

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
    assert snapshot.latest_out_path == out_path.resolve()
    assert snapshot.final_reason == "terminated_normally"
    assert snapshot.elapsed == 3661.0
    assert snapshot.elapsed_text == "1h 01m"


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
    assert snapshot.latest_out_path == out_path.resolve()
    assert snapshot.final_reason == "tracked_completion"


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


def test_sort_snapshots_by_started_handles_invalid_timestamps(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    ordered = sort_snapshots_by_started(
        [
            _snapshot(reaction_dir, name="invalid", started_at="not-a-time"),
            _snapshot(reaction_dir, name="later", started_at="2026-01-10T12:00:00Z"),
            _snapshot(reaction_dir, name="earlier", started_at="2026-01-10T11:00:00+00:00"),
        ]
    )

    assert [snapshot.name for snapshot in ordered] == ["earlier", "later", "invalid"]


def test_sort_snapshots_by_completed_uses_updated_at_fallback_and_invalid_last(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    ordered = sort_snapshots_by_completed(
        [
            _snapshot(
                reaction_dir,
                name="fallback",
                started_at="2026-01-10T09:00:00+00:00",
                updated_at="2026-01-10T12:30:00+00:00",
                completed_at="bad",
            ),
            _snapshot(
                reaction_dir,
                name="completed",
                started_at="2026-01-10T09:30:00+00:00",
                updated_at="2026-01-10T12:00:00+00:00",
                completed_at="2026-01-10T12:00:00+00:00",
            ),
            _snapshot(
                reaction_dir,
                name="invalid",
                started_at="2026-01-10T08:00:00+00:00",
                updated_at="also-bad",
                completed_at="",
            ),
        ]
    )

    assert [snapshot.name for snapshot in ordered] == ["fallback", "completed", "invalid"]
