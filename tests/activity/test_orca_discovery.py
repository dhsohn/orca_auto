from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from orca_auto import cli_queue
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


def test_ordinary_activity_uses_index_without_recursive_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    config = tmp_path / "config.yaml"
    config.write_text(f"runs_root: {root}\n")
    _write_run(root, "tracked", indexed=True)
    _write_run(root, "untracked", indexed=False)

    def forbidden_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("ordinary queue list traversed the run tree")

    monkeypatch.setattr(run_snapshot, "iter_production_runs_artifacts", forbidden_scan)
    result = list_activities(config_path=str(config), runs_root=root, limit=1)
    assert [item["activity_id"] for item in result["activities"]] == ["tracked"]


def test_explicit_refresh_discovers_and_indexes_unindexed_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    config = tmp_path / "config.yaml"
    config.write_text(f"runs_root: {root}\n")
    _write_run(root, "tracked", indexed=True)
    _write_run(root, "untracked", indexed=False)
    result = list_activities(config_path=str(config), runs_root=root, refresh=True)
    assert {item["activity_id"] for item in result["activities"]} == {"tracked", "untracked"}
    # The discovery is now an index row, so the next ordinary list needs no walk.
    assert {row.job_id for row in list_job_locations(root)} == {"tracked", "untracked"}
    monkeypatch.setattr(
        run_snapshot,
        "iter_production_runs_artifacts",
        lambda *args, **kwargs: pytest.fail("ordinary queue list traversed the run tree"),
    )
    plain = list_activities(config_path=str(config), runs_root=root)
    assert {item["activity_id"] for item in plain["activities"]} == {"tracked", "untracked"}


def test_refresh_request_has_no_status_or_engine_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(f"runs_root: {root}\n")
    requests: list[dict[str, object]] = []

    def fake_list_activities(**kwargs: object) -> dict[str, object]:
        requests.append(kwargs)
        return {"count": 0, "active_simulations": 0, "activities": [], "sources": {}}

    monkeypatch.setattr(cli_queue, "list_activities", fake_list_activities)

    assert cli_queue.cmd_queue_list(Namespace(config=str(config), refresh=True, json=True)) == 0
    [request] = requests
    assert request["statuses"] == ()
    assert request["refresh"] is True
    assert "engines" not in request
    capsys.readouterr()


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
