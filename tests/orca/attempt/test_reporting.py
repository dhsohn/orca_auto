from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.core.queue.generation_owner import bind_direct_generation_owner
from orca_auto.orca.attempt.reporting import (
    AttemptDecision,
    _print_run_summary,
    build_final_result,
    decide_attempt_outcome,
    exit_with_result,
    last_out_path_from_state,
    parse_analyzer_status,
)
from orca_auto.orca.engine_runner import executable_identity
from orca_auto.orca.notifications import finished_notification_already_sent
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus


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


def test_parse_analyzer_status_accepts_members_and_values_only() -> None:
    assert parse_analyzer_status(AnalyzerStatus.COMPLETED) is AnalyzerStatus.COMPLETED
    assert parse_analyzer_status("completed") is AnalyzerStatus.COMPLETED
    assert parse_analyzer_status("invalid") is None


@pytest.mark.parametrize(
    ("analyzer_status", "reason", "expected"),
    [
        (AnalyzerStatus.COMPLETED, "normal_termination", (RunStatus.COMPLETED, 0)),
        ("completed", "ts_criteria_met", (RunStatus.COMPLETED, 0)),
        (AnalyzerStatus.ERROR_MULTIPLICITY_IMPOSSIBLE.value, "bad_spin", (RunStatus.FAILED, 1)),
        (AnalyzerStatus.UNKNOWN_FAILURE.value, "error_termination", (RunStatus.FAILED, 1)),
        (AnalyzerStatus.INCOMPLETE.value, "run_incomplete", (RunStatus.FAILED, 1)),
        (AnalyzerStatus.ERROR_MEMORY, "out_of_memory", (RunStatus.FAILED, 1)),
        (AnalyzerStatus.GEOM_NOT_CONVERGED, "geometry_not_converged", (RunStatus.FAILED, 1)),
        ("invalid", "still_running", (RunStatus.FAILED, 1)),
    ],
)
def test_decide_attempt_outcome_completes_only_an_analyzer_completed_attempt(
    analyzer_status: AnalyzerStatus | str, reason: str, expected: tuple[RunStatus, int]
) -> None:
    assert decide_attempt_outcome(
        analyzer_status=analyzer_status, analyzer_reason=reason
    ) == AttemptDecision(run_status=expected[0], reason=reason, exit_code=expected[1])


def test_exit_with_result_publishes_state_and_reports_and_prints_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
        extra={"skipped_execution": True},
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
    assert capsys.readouterr().out.splitlines() == [
        "status: completed",
        f"job_dir: {reaction_dir}",
        f"selected_inp: {selected_inp}",
        "attempt_count: 0",
        "reason: normal_termination",
        f"run_state: {reaction_dir / 'job_state.json'}",
        f"report_json: {generation / 'machine.json'}",
    ]
    assert machine["lifecycle"]["outcome"] == "succeeded"
    assert machine["payload"]["data"]["summary"]["status"] == "completed"
    assert "finished_notification_sent_at" not in saved["final_result"]
    assert "finished_notification_claimed_at" not in saved["final_result"]
    assert saved["final_result"]["resumed"]
    assert saved["final_result"]["skipped_execution"]


def test_run_summary_prints_known_keys_only(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "status": "completed",
        "reaction_dir": "/tmp/rxn",
        "selected_inp": "/tmp/rxn/rxn.inp",
        "attempt_count": 1,
        "reason": "normal_termination",
        "run_state": "/tmp/rxn/job_state.json",
        "extra_unknown_key": "ignored",
    }
    _print_run_summary(payload)
    output = capsys.readouterr().out
    assert "status: completed" in output
    assert "attempt_count: 1" in output
    assert "extra_unknown_key" not in output


def test_run_summary_labels_reaction_dir_as_job_dir(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "status": "completed",
        "reaction_dir": "/tmp/rxn",
        "selected_inp": "rxn.inp",
        "attempt_count": 2,
        "reason": "normal_termination",
        "report_json": "/tmp/report.json",
        "ignored": "value",
    }

    _print_run_summary(payload)

    output = capsys.readouterr().out
    assert "status: completed" in output
    assert "job_dir: /tmp/rxn" in output
    assert "report_json: /tmp/report.json" in output
    assert "ignored" not in output


# -- logging ----------------------------------------------------------------
