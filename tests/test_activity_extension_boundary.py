from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto import activity
from orca_auto.core.queue.persistence import entry_to_dict
from orca_auto.core.queue.types import QueueEntry, QueueStatus


def _standalone_queue(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runs"
    root.mkdir()
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {root}\n", encoding="utf-8")
    entry = QueueEntry(
        queue_id="standalone-job",
        task_id="standalone-task",
        app_name="orca_auto_orca",
        engine="orca",
        task_kind="orca_run_inp",
        status=QueueStatus.PENDING,
        priority=0,
        enqueued_at="2026-01-01T00:00:00Z",
        metadata={"reaction_dir": str(root / "standalone")},
    )
    (root / "queue.json").write_text(json.dumps([entry_to_dict(entry)]), encoding="utf-8")
    return config, root


@pytest.mark.parametrize("evidence", ["registry", "workspace"])
def test_unrelated_retired_state_does_not_block_standalone_orca(
    tmp_path: Path, evidence: str
) -> None:
    config, root = _standalone_queue(tmp_path)
    if evidence == "registry":
        marker = root / "workflow_registry.json"
    else:
        retired = root / "retired"
        retired.mkdir()
        marker = retired / "workflow.json"
    marker.write_text("preserved retired evidence", encoding="utf-8")
    before = (root / "queue.json").read_bytes()
    payload = activity.list_activities(shared_config=str(config), refresh=True)
    assert [row["activity_id"] for row in payload["activities"]] == ["standalone-job"]
    assert activity.clear_activities(shared_config=str(config))["total_cleared"] == 0
    assert (root / "queue.json").read_bytes() == before
    assert marker.read_text(encoding="utf-8") == "preserved retired evidence"
