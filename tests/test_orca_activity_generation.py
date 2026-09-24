from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from orca_auto.activity._orca import orca_records
from orca_auto.activity._orca_index import query_listing
from orca_auto.core.activity import ActivityListRequest, ActivitySourceRequest
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.orca.config import load_config
from orca_auto.orca.execution_binding import orca_execution_provenance
from orca_auto.orca.queue import adapter
from orca_auto.orca.queue.terminal_replay import TERMINAL_REPLAY_METADATA_KEY
from orca_auto.orca.state import new_state, write_state
from orca_auto.orca.submission import create_queued_submission


@pytest.mark.parametrize("claimed", [False, True], ids=["pending", "running"])
@pytest.mark.parametrize("indexed", [False, True], ids=["discovery", "indexed-query"])
@pytest.mark.parametrize(
    "matching_state", [False, True], ids=["prior-generation", "current-generation"]
)
def test_activity_borrows_state_only_from_its_queue_generation(
    tmp_path: Path,
    claimed: bool,
    matching_state: bool,
    indexed: bool,
) -> None:
    runs_root = tmp_path / "runs"
    job_dir = runs_root / "job"
    job_dir.mkdir(parents=True)
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    executable = tmp_path / "fake-orca"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        f"runs_root: {runs_root}\norca:\n  paths:\n    orca_executable: {executable}\n"
    )
    cfg = load_config(str(config_path))
    args = Namespace(force=False, priority=10)
    previous = create_queued_submission(cfg, args, job_dir, selected_inp=selected).entry
    previous = adapter.dequeue_next(runs_root)
    assert previous is not None
    state = new_state(job_dir, Path(previous.metadata["selected_inp"]))
    state["job_id"] = previous.task_id
    state["queue_id"] = previous.queue_id
    state["queue_generation"] = queue_entry_generation_token(previous)
    state["status"] = "completed"
    state["execution_provenance"] = orca_execution_provenance(
        previous.metadata["execution_snapshot"]
    )
    state["final_result"] = {
        "status": "completed",
        "completed_at": "2026-01-01T00:00:00+00:00",
        "reason": "completed",
        "analyzer_status": "completed",
        "last_out_path": "",
    }
    write_state(job_dir, state)
    assert adapter.mark_completed(runs_root, previous.queue_id, run_id=state["run_id"])
    assert adapter.update_metadata(
        runs_root, previous.queue_id, {TERMINAL_REPLAY_METADATA_KEY: None}
    )
    current = create_queued_submission(cfg, args, job_dir, selected_inp=selected).entry
    if claimed:
        current = adapter.dequeue_next(runs_root)
        assert current is not None
    if matching_state:
        state["run_id"] = "current-run"
        state["job_id"] = current.task_id
        state["queue_id"] = current.queue_id
        state["queue_generation"] = queue_entry_generation_token(current)
        state["selected_inp"] = current.metadata["selected_inp"]
        state["execution_provenance"] = orca_execution_provenance(
            current.metadata["execution_snapshot"]
        )
        write_state(job_dir, state)

    rows = (
        list(
            query_listing(
                runs_root, ActivityListRequest(ActivitySourceRequest(), indexed=True)
            ).records
        )
        if indexed
        else orca_records(config_path=str(config_path))
    )
    current_activity = next(row for row in rows if row.activity_id == current.queue_id)

    # A completed state can precede its parent's queue transition, but only for
    # the same generation. A reused job root cannot lend its predecessor's result.
    assert current_activity.status == ("completed" if matching_state and claimed else "pending")
    assert current_activity.updated_at == (
        "2026-01-01T00:00:00+00:00" if matching_state else current.started_at or current.enqueued_at
    )
    assert len(rows) == 2
