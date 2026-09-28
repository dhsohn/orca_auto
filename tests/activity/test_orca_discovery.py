from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.activity import list_activities
from orca_auto.core.indexing import JobLocationRecord, list_job_locations, upsert_job_location
from orca_auto.core.queue.persistence import save_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca import run_snapshot
from orca_auto.orca.state import save_state


def _write_run(root: Path, name: str, *, indexed: bool) -> None:
    job = root / name
    job.mkdir(parents=True)
    save_state(
        job,
        {
            "run_id": name,
            "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "selected_inp": str(job / "calc.inp"),
            "attempts": [],
            "final_result": {"completed_at": "2026-01-01T01:00:00+00:00"},
        },
    )
    if indexed:
        upsert_job_location(
            root,
            JobLocationRecord(
                job_id=name,
                app_name="orca_auto_orca",
                job_type="orca_sp",
                status="completed",
                original_run_dir=str(job),
                latest_known_path=str(job),
            ),
        )


def test_listing_reads_neither_the_location_index_nor_the_run_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    config = tmp_path / "config.yaml"
    config.write_text(f"runs_root: {root}\n")
    _write_run(root, "tracked", indexed=True)
    _write_run(root, "untracked", indexed=False)
    save_entries(
        root,
        [
            QueueEntry(
                queue_id="queue-id",
                app_name="orca_auto_orca",
                task_id="task-id",
                task_kind="orca_run_inp",
                engine="orca",
                status=QueueStatus.COMPLETED,
                metadata={"run_id": "untracked", "reaction_dir": str(root / "untracked")},
            )
        ],
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("queue list read beyond the queue rows' own states")

    monkeypatch.setattr(run_snapshot, "iter_production_runs_artifacts", forbidden)
    monkeypatch.setattr(run_snapshot, "list_job_location_records", forbidden)
    result = list_activities(config_path=str(config), runs_root=root)
    # The indexed run has no queue row, so it is not listed.
    assert [item["activity_id"] for item in result["activities"]] == ["queue-id"]
    assert [row.job_id for row in list_job_locations(root)] == ["tracked"]


def test_queue_known_run_does_not_require_an_index_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    config = tmp_path / "config.yaml"
    config.write_text(f"runs_root: {root}\n")
    _write_run(root, "untracked", indexed=False)
    entry = QueueEntry(
        queue_id="queue-id",
        app_name="orca_auto_orca",
        task_id="task-id",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.RUNNING,
        metadata={"run_id": "untracked", "reaction_dir": str(root / "untracked")},
    )
    save_entries(root, [entry])
    result = list_activities(config_path=str(config), runs_root=root)
    assert len(result["activities"]) == 1
    assert result["activities"][0]["status"] == "completed"
    assert result["activities"][0]["updated_at"] == "2026-01-01T01:00:00+00:00"
