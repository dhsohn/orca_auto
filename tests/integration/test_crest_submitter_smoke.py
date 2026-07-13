from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import get_job_location
from orca_auto.core.queue import list_queue
from orca_auto.flow.adapters.crest import load_crest_artifact_contract
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_cmd
from orca_auto.flow.submitters import crest as crest_submitter
from tests.engine_artifact_helpers import engine_payload as _engine_payload
from tests.engine_artifact_helpers import status as _status
from tests.engine_process_helpers import process_one_crest_for_test


def _queue_status(entry: Any) -> str:
    return str(getattr(getattr(entry, "status", None), "value", "")).strip()


def test_crest_submitter_roundtrip_smoke(
    smoke_workspace: Any,
    crest_job: Path,
    capsys: Any,
) -> None:
    submission = crest_submitter.submit_job_dir(
        job_dir=str(crest_job),
        priority=5,
        config_path=str(smoke_workspace.crest_config_path),
    )

    assert submission["status"] == "submitted"
    assert submission["parsed_stdout"]["status"] == "queued"
    assert submission["job_id"]
    assert submission["queue_id"]

    queue_entries = list_queue(smoke_workspace.crest_allowed_root)
    assert len(queue_entries) == 1
    assert queue_entries[0].task_id == submission["job_id"]
    assert queue_entries[0].queue_id == submission["queue_id"]
    assert _queue_status(queue_entries[0]) == "pending"

    assert (
        process_one_crest_for_test(
            crest_queue_cmd,
            crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path)),
        )
        == "processed"
    )
    worker_output = capsys.readouterr().out
    assert f"queue_id: {submission['queue_id']}" in worker_output
    assert f"job_id: {submission['job_id']}" in worker_output
    assert "status: completed" in worker_output

    record = get_job_location(smoke_workspace.crest_allowed_root, submission["job_id"])
    assert record is not None
    assert record.app_name == "orca_auto_crest"
    assert record.status == "completed"
    assert record.original_run_dir == str(crest_job.resolve())
    assert record.latest_known_path == str(crest_job.resolve())

    artifact_dir = crest_job.resolve()
    assert artifact_dir.exists()
    assert not (crest_job / "organized_ref.json").exists()
    assert (artifact_dir / "job_state.json").exists()
    assert (artifact_dir / "job_report.json").exists()
    assert (artifact_dir / "job_report.md").exists()
    assert (artifact_dir / "crest_conformers.xyz").exists()
    assert (artifact_dir / "crest_best.xyz").exists()

    report_payload = json.loads((artifact_dir / "job_report.json").read_text(encoding="utf-8"))
    assert _status(report_payload)["state"] == "completed"
    assert _engine_payload(report_payload)["mode"] == "standard"
    assert _engine_payload(report_payload)["retained_conformer_count"] == 2
    assert [
        Path(path).name for path in _engine_payload(report_payload)["retained_conformer_paths"]
    ] == ["crest_conformers.xyz", "crest_best.xyz"]

    contract = load_crest_artifact_contract(
        crest_index_root=smoke_workspace.crest_allowed_root,
        target=submission["job_id"],
    )
    assert contract.status == "completed"
    assert contract.mode == "standard"
    assert contract.retained_conformer_count == 2
    assert [Path(path).name for path in contract.retained_conformer_paths] == [
        "crest_conformers.xyz",
        "crest_best.xyz",
    ]

    queue_entries_after = list_queue(smoke_workspace.crest_allowed_root)
    assert len(queue_entries_after) == 1
    assert _queue_status(queue_entries_after[0]) == "completed"

    admission_path = smoke_workspace.admission_root / "admission_slots.json"
    if admission_path.exists():
        assert json.loads(admission_path.read_text(encoding="utf-8")) == []
