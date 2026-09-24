from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca.attempt.reporting import (
    build_final_result,
    exit_with_result,
    finished_notification_already_sent,
    last_out_path_from_state,
)
from orca_auto.orca.engine_runner import executable_identity
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import RunFinishedNotification


def test_finished_notification_marker_reads_canonical_state() -> None:
    assert finished_notification_already_sent(
        {"final_result": {"finished_notification_sent_at": "2026-07-11T00:00:00Z"}}
    )
    assert not finished_notification_already_sent({"final_result": {}})


def test_last_out_path_from_state_defensive_cases() -> None:
    assert last_out_path_from_state({"attempts": []}) is None
    assert last_out_path_from_state({"attempts": ["bad"]}) is None
    assert last_out_path_from_state({"attempts": [{"out_path": "   "}]}) is None
    assert last_out_path_from_state({"attempts": [{"out_path": "/tmp/run.out"}]}) == "/tmp/run.out"


def test_last_out_path_prefers_newer_interrupted_retry_publication() -> None:
    state = {
        "attempts": [{"index": 1, "out_path": "/tmp/run.out"}],
        "scratch_publications": [
            {
                "attempt_index": 2,
                "inp_path": "/tmp/run.retry01.inp",
                "publication": {"published_files": ["run.retry01.gbw", "run.retry01.out"]},
            }
        ],
    }

    assert last_out_path_from_state(state) == "/tmp/run.retry01.out"


def test_build_final_result_keeps_supported_extra_fields_only() -> None:
    result = build_final_result(
        status=RunStatus.FAILED,
        analyzer_status=AnalyzerStatus.INCOMPLETE,
        reason="runner_failed",
        last_out_path="/tmp/run.out",
        resumed=False,
        extra={
            "skipped_execution": True,
            "runner_error": "boom",
            "ignored": 123,
        },
    )

    assert result["status"] == "failed"
    assert result["analyzer_status"] == "incomplete"
    assert result["skipped_execution"]
    assert result["runner_error"] == "boom"
    assert "ignored" not in result


def test_exit_with_result_writes_state_reports_and_finished_notification(tmp_path: Path) -> None:
    reaction_dir = tmp_path
    generation = reaction_dir / "20260714-224054-959479f2"
    generation.mkdir()
    selected_inp = generation / "rxn.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    generation_status = generation.stat()
    reaction_status = reaction_dir.stat()
    owner_token = "attempt-report-owner-token-0001"
    bind_direct_generation_owner(
        reaction_dir,
        namespace=generation.name,
        expected_job_identity=(reaction_status.st_dev, reaction_status.st_ino),
        expected_generation_identity=(generation_status.st_dev, generation_status.st_ino),
        owner_token=owner_token,
    )
    state = new_state(reaction_dir, selected_inp)
    state["execution_provenance"] = {
        "execution_dir": str(generation),
        "execution_dir_identity": {
            "device": generation_status.st_dev,
            "inode": generation_status.st_ino,
        },
        "generation_owner_token": owner_token,
        "bound_selected_identity": executable_identity(selected_inp),
    }
    emitted_payloads: list[dict[str, Any]] = []
    finished_notifications: list[RunFinishedNotification] = []

    def notify_finished(payload: RunFinishedNotification) -> bool:
        finished_notifications.append(payload)
        return True

    rc = exit_with_result(
        reaction_dir,
        state,
        selected_inp,
        status=RunStatus.COMPLETED,
        analyzer_status=AnalyzerStatus.COMPLETED,
        reason="normal_termination",
        last_out_path=str(reaction_dir / "rxn.out"),
        resumed=True,
        exit_code=0,
        emit=lambda payload: emitted_payloads.append(payload),
        extra={"skipped_execution": True},
        notify_finished=notify_finished,
    )

    saved = load_state(reaction_dir)
    machine = json.loads((generation / "machine.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert not (reaction_dir / "machine.json").exists()
    assert saved is not None
    assert saved["status"] == "completed"
    assert saved["final_result"] is not None
    assert saved["final_result"]["reason"] == "normal_termination"
    assert saved["final_result"]["last_out_path"] == str(reaction_dir / "rxn.out")
    assert len(emitted_payloads) == 1
    assert emitted_payloads[0]["status"] == "completed"
    assert emitted_payloads[0]["run_state"] == str(reaction_dir / "job_state.json")
    assert emitted_payloads[0]["report_json"] == str(generation / "machine.json")
    assert machine["lifecycle"]["outcome"] == "succeeded"
    assert machine["payload"]["data"]["summary"]["status"] == "completed"
    assert "finished_notification_sent_at" in saved["final_result"]
    assert len(finished_notifications) == 1
    assert finished_notifications[0]["resumed"]
    assert finished_notifications[0]["skipped_execution"]
