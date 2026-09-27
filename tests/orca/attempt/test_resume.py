from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from orca_auto.orca.attempt import resume as attempt_resume
from orca_auto.orca.attempt.resume import (
    is_resumable_state,
    load_or_create_state,
    recover_crashed_state,
    settle_from_recorded_attempt,
    state_matches_selected,
)
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import RunFinalResult, RunState
from tests.conftest import write_run_state


def _saved_final(reaction_dir: Path) -> RunFinalResult:
    saved = load_state(reaction_dir)
    assert saved is not None and saved["final_result"] is not None
    return saved["final_result"]


def test_state_matches_selected_handles_blank_unresolvable_and_matching_paths(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    assert not state_matches_selected({}, selected_inp)
    assert not state_matches_selected({"selected_inp": "   "}, selected_inp)
    assert not state_matches_selected({"selected_inp": "calc\0.inp"}, selected_inp)
    assert not state_matches_selected({"selected_inp": str(tmp_path / "other.inp")}, selected_inp)
    assert state_matches_selected({"selected_inp": str(selected_inp)}, selected_inp)
    assert state_matches_selected({"selected_inp": str(tmp_path / "." / "calc.inp")}, selected_inp)


def test_recover_crashed_state_closes_an_active_state_as_crashed_recovery(
    tmp_path: Path,
) -> None:
    # Called under the run lock (exclusive owner), a running/retrying state is
    # a crash and is reconciled to failed/crashed_recovery.
    reaction_dir = tmp_path / "rxn"
    write_run_state(reaction_dir, status="running", run_id="run_active")

    assert recover_crashed_state(reaction_dir) is True

    state = load_state(reaction_dir)
    assert state is not None
    assert state["run_id"] == "run_active"
    assert state["status"] == "failed"
    assert state["final_result"] == {
        "status": "failed",
        "reason": "crashed_recovery",
        "analyzer_status": "incomplete",
    }
    assert recover_crashed_state(reaction_dir) is False


def test_only_active_and_crash_recovered_states_are_resumable() -> None:
    assert is_resumable_state({"status": RunStatus.RUNNING.value})
    assert is_resumable_state({"status": RunStatus.RETRYING.value})
    assert is_resumable_state(
        {"status": RunStatus.FAILED.value, "final_result": {"reason": " crashed_recovery "}}
    )
    # Reasons of the removed attempt-level resume stay readable but never resume.
    for reason in ("interrupted_by_user", "worker_shutdown", "orca_crash"):
        assert not is_resumable_state(
            {"status": RunStatus.FAILED.value, "final_result": {"reason": reason}}
        )
    assert not is_resumable_state(
        cast(RunState, {"status": RunStatus.FAILED.value, "final_result": []})
    )
    assert not is_resumable_state(
        cast(RunState, {"status": RunStatus.FAILED.value, "final_result": {"reason": 123}})
    )
    assert not is_resumable_state({"status": RunStatus.COMPLETED.value})


def test_load_or_create_state_starts_new_state_without_a_matching_one(tmp_path: Path) -> None:
    selected_inp = tmp_path / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    state, resumed = load_or_create_state(tmp_path, selected_inp)

    assert not resumed
    assert state["selected_inp"] == str(selected_inp)
    assert state["status"] == RunStatus.CREATED.value
    saved = load_state(tmp_path)
    assert saved is not None and saved["run_id"] == state["run_id"]

    write_run_state(tmp_path, status="running", run_id="run_other", selected_inp=tmp_path / "x.inp")
    state, resumed = load_or_create_state(tmp_path, selected_inp)

    assert not resumed
    assert state["run_id"] != "run_other"


def test_load_or_create_state_resumes_a_crash_recovered_state(tmp_path: Path) -> None:
    selected_inp = tmp_path / "calc.inp"
    write_run_state(
        tmp_path,
        status="failed",
        run_id="run_crashed",
        selected_inp=selected_inp,
        final_result={
            "status": "failed",
            "analyzer_status": "incomplete",
            "reason": "crashed_recovery",
            "completed_at": "",
            "last_out_path": None,
        },
    )

    state, resumed = load_or_create_state(tmp_path, selected_inp)

    assert resumed
    assert state["run_id"] == "run_crashed"
    assert state["final_result"] is None
    assert state["attempts"] == []
    saved = load_state(tmp_path)
    assert saved is not None and saved["final_result"] is None


@pytest.mark.parametrize(
    ("status", "reason"),
    [("completed", "normal_termination"), ("failed", "interrupted_by_user")],
)
def test_load_or_create_state_replaces_a_settled_state(
    tmp_path: Path, status: str, reason: str
) -> None:
    selected_inp = tmp_path / "calc.inp"
    write_run_state(
        tmp_path,
        status=status,
        run_id="run_settled",
        selected_inp=selected_inp,
        final_result={
            "status": status,
            "analyzer_status": "completed" if status == "completed" else "incomplete",
            "reason": reason,
            "completed_at": "",
            "last_out_path": None,
        },
    )

    state, resumed = load_or_create_state(tmp_path, selected_inp)

    assert not resumed
    assert state["run_id"] != "run_settled"
    assert state["status"] == RunStatus.CREATED.value


def test_settle_from_recorded_attempt_publishes_the_recorded_verdict(tmp_path: Path) -> None:
    selected_inp = tmp_path / "rxn.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    state = new_state(tmp_path, selected_inp)
    state["attempts"].append(
        {
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "out_path": str(tmp_path / "rxn.out"),
        }
    )

    assert settle_from_recorded_attempt(tmp_path, selected_inp, state) == 0

    final = _saved_final(tmp_path)
    assert final["status"] == "completed"
    assert final["reason"] == "normal_termination"
    assert final["last_out_path"] == str(tmp_path / "rxn.out")
    assert final["resumed"] is True


def test_attempt_resume_text_helper_covers_existing_and_missing_values() -> None:
    assert attempt_resume._as_non_empty_text(" hello ") == "hello"
    assert attempt_resume._as_non_empty_text("   ") is None
    assert attempt_resume._as_non_empty_text(123) is None


def test_settle_from_recorded_attempt_covers_malformed_and_defaulted_terminal_paths(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    assert (
        settle_from_recorded_attempt(tmp_path, selected_inp, new_state(tmp_path, selected_inp))
        is None
    )
    assert (
        settle_from_recorded_attempt(tmp_path, selected_inp, cast(RunState, {"attempts": "bad"}))
        is None
    )
    assert (
        settle_from_recorded_attempt(tmp_path, selected_inp, cast(RunState, {"attempts": ["bad"]}))
        is None
    )

    incomplete = new_state(tmp_path, selected_inp)
    incomplete["attempts"] = [
        {"analyzer_status": AnalyzerStatus.INCOMPLETE.value, "analyzer_reason": "still_running"}
    ]
    assert settle_from_recorded_attempt(tmp_path, selected_inp, incomplete) == 1
    assert _saved_final(tmp_path)["reason"] == "still_running"

    defaulted = new_state(tmp_path, selected_inp)
    defaulted["attempts"] = [
        {"index": 1, "analyzer_status": "completed", "out_path": str(tmp_path / "state.out")},
        {"analyzer_status": " ", "analyzer_reason": " ", "out_path": " "},
    ]
    assert settle_from_recorded_attempt(tmp_path, selected_inp, defaulted) == 1

    final = _saved_final(tmp_path)
    assert final["status"] == RunStatus.FAILED.value
    assert final["analyzer_status"] == AnalyzerStatus.INCOMPLETE.value
    assert final["reason"] == "resume_last_attempt"
    assert final["last_out_path"] == str(tmp_path / "state.out")
