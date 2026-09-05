from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

from orca_auto.orca.state_machine import (
    AttemptDecision,
    decide_attempt_outcome,
    is_resumable_state,
    load_or_create_state,
    parse_analyzer_status,
    state_matches_selected,
)
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import RunState


def test_parse_analyzer_status_and_decide_attempt_outcome_cover_terminal_paths() -> None:
    assert parse_analyzer_status(AnalyzerStatus.COMPLETED) is AnalyzerStatus.COMPLETED
    assert parse_analyzer_status("completed") is AnalyzerStatus.COMPLETED
    assert parse_analyzer_status("invalid") is None

    assert decide_attempt_outcome(
        analyzer_status=AnalyzerStatus.COMPLETED,
        analyzer_reason="normal_termination",
    ) == AttemptDecision(
        run_status=RunStatus.COMPLETED,
        reason="normal_termination",
        exit_code=0,
    )
    assert decide_attempt_outcome(
        analyzer_status=AnalyzerStatus.ERROR_MULTIPLICITY_IMPOSSIBLE.value,
        analyzer_reason="bad_spin",
    ) == AttemptDecision(
        run_status=RunStatus.FAILED,
        reason="bad_spin",
        exit_code=1,
    )
    assert decide_attempt_outcome(
        analyzer_status=AnalyzerStatus.UNKNOWN_FAILURE.value,
        analyzer_reason="error_termination",
    ) == AttemptDecision(
        run_status=RunStatus.FAILED,
        reason="error_termination",
        exit_code=1,
    )
    assert decide_attempt_outcome(
        analyzer_status=AnalyzerStatus.INCOMPLETE.value,
        analyzer_reason="run_incomplete",
    ) == AttemptDecision(
        run_status=RunStatus.FAILED,
        reason="run_incomplete",
        exit_code=1,
    )
    assert decide_attempt_outcome(
        analyzer_status=AnalyzerStatus.INCOMPLETE.value,
        analyzer_reason="still_running",
    ) == AttemptDecision(run_status=RunStatus.FAILED, reason="still_running", exit_code=1)


def test_state_matches_selected_handles_blank_and_resolver_errors(tmp_path: Path) -> None:
    selected_inp = tmp_path / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    assert not state_matches_selected({}, selected_inp, to_resolved_local=lambda raw: Path(raw))
    assert not state_matches_selected(
        {"selected_inp": "   "},
        selected_inp,
        to_resolved_local=lambda raw: Path(raw),
    )
    assert state_matches_selected(
        {"selected_inp": str(selected_inp)},
        selected_inp,
        to_resolved_local=lambda raw: Path(raw).resolve(),
    )
    assert not state_matches_selected(
        {"selected_inp": str(selected_inp)},
        selected_inp,
        to_resolved_local=lambda raw: (_ for _ in ()).throw(RuntimeError(raw)),
    )


def test_is_resumable_state_covers_running_retrying_failed_and_non_resumable_cases() -> None:
    assert is_resumable_state({"status": RunStatus.RUNNING.value})
    assert is_resumable_state({"status": RunStatus.RETRYING.value})
    assert is_resumable_state(
        {
            "status": RunStatus.FAILED.value,
            "final_result": {"reason": " interrupted_by_user "},
        }
    )
    assert is_resumable_state(
        {
            "status": RunStatus.FAILED.value,
            "final_result": {"reason": "worker_shutdown"},
        }
    )
    assert is_resumable_state(
        {
            "status": RunStatus.FAILED.value,
            "final_result": {"reason": "crashed_recovery"},
        }
    )
    assert not is_resumable_state(
        {
            "status": RunStatus.FAILED.value,
            "final_result": {"reason": "orca_crash"},
        }
    )
    assert not is_resumable_state(
        cast(RunState, {"status": RunStatus.FAILED.value, "final_result": []})
    )
    assert not is_resumable_state(
        cast(
            RunState,
            {
                "status": RunStatus.FAILED.value,
                "final_result": {"reason": 123},
            },
        )
    )
    assert not is_resumable_state({"status": RunStatus.COMPLETED.value})


def test_load_or_create_state_creates_new_state_for_missing_or_mismatched_selection(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    selected_inp = reaction_dir / "calc.inp"
    replacement_state: RunState = {
        "run_id": "run_new",
        "selected_inp": str(selected_inp),
        "status": RunStatus.CREATED.value,
        "attempts": [],
        "final_result": None,
    }

    with (
        patch("orca_auto.orca.state_machine.load_state", return_value=None),
        patch(
            "orca_auto.orca.state_machine.new_state",
            return_value=dict(replacement_state),
        ) as new_state_mock,
        patch("orca_auto.orca.state_machine.save_state") as save_state_mock,
    ):
        state, resumed = load_or_create_state(
            reaction_dir,
            selected_inp,
            to_resolved_local=lambda raw: Path(raw).resolve(),
        )

    assert not resumed
    assert state["run_id"] == "run_new"
    new_state_mock.assert_called_once_with(reaction_dir, selected_inp)
    save_state_mock.assert_called_once_with(reaction_dir, state)

    mismatched_loaded_state: RunState = {
        "run_id": "run_old",
        "selected_inp": str(reaction_dir / "other.inp"),
        "status": RunStatus.RUNNING.value,
        "attempts": [],
        "final_result": None,
    }
    with (
        patch("orca_auto.orca.state_machine.load_state", return_value=mismatched_loaded_state),
        patch(
            "orca_auto.orca.state_machine.new_state",
            return_value=dict(replacement_state),
        ) as new_state_mock,
        patch("orca_auto.orca.state_machine.save_state"),
    ):
        state, resumed = load_or_create_state(
            reaction_dir,
            selected_inp,
            to_resolved_local=lambda raw: Path(raw).resolve(),
        )

    assert not resumed
    assert state["run_id"] == "run_new"
    new_state_mock.assert_called_once_with(reaction_dir, selected_inp)


def test_load_or_create_state_resumes_or_resets_and_normalizes_attempts(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    resumable_state = {
        "run_id": "run_resume",
        "selected_inp": str(selected_inp),
        "status": RunStatus.FAILED.value,
        "attempts": "bad",
        "final_result": {"reason": "interrupted_by_user"},
    }
    with (
        patch("orca_auto.orca.state_machine.load_state", return_value=resumable_state),
        patch(
            "orca_auto.orca.state_machine.new_state",
        ) as new_state_mock,
        patch("orca_auto.orca.state_machine.save_state") as save_state_mock,
    ):
        state, resumed = load_or_create_state(
            reaction_dir,
            selected_inp,
            to_resolved_local=lambda raw: Path(raw).resolve(),
        )

    assert resumed
    assert state["final_result"] is None
    assert state["attempts"] == []
    new_state_mock.assert_not_called()
    save_state_mock.assert_called_once_with(reaction_dir, state)

    resumable_state = {
        "run_id": "run_worker_shutdown",
        "selected_inp": str(selected_inp),
        "status": RunStatus.FAILED.value,
        "attempts": [],
        "final_result": {"reason": "worker_shutdown"},
    }
    with (
        patch("orca_auto.orca.state_machine.load_state", return_value=resumable_state),
        patch(
            "orca_auto.orca.state_machine.new_state",
        ) as new_state_mock,
        patch("orca_auto.orca.state_machine.save_state") as save_state_mock,
    ):
        state, resumed = load_or_create_state(
            reaction_dir,
            selected_inp,
            to_resolved_local=lambda raw: Path(raw).resolve(),
        )

    assert resumed
    assert state["final_result"] is None
    assert state["attempts"] == []
    new_state_mock.assert_not_called()
    save_state_mock.assert_called_once_with(reaction_dir, state)

    reset_state = {
        "run_id": "run_done",
        "selected_inp": str(selected_inp),
        "status": RunStatus.COMPLETED.value,
        "attempts": [{"inp_path": str(selected_inp)}],
        "final_result": {"reason": "normal_termination"},
    }
    replacement_state: RunState = {
        "run_id": "run_reset",
        "selected_inp": str(selected_inp),
        "status": RunStatus.CREATED.value,
        "attempts": [],
        "final_result": None,
    }
    with (
        patch("orca_auto.orca.state_machine.load_state", return_value=reset_state),
        patch(
            "orca_auto.orca.state_machine.new_state",
            return_value=dict(replacement_state),
        ) as new_state_mock,
        patch("orca_auto.orca.state_machine.save_state"),
    ):
        state, resumed = load_or_create_state(
            reaction_dir,
            selected_inp,
            to_resolved_local=lambda raw: Path(raw).resolve(),
        )

    assert not resumed
    assert state["run_id"] == "run_reset"
    new_state_mock.assert_called_once_with(reaction_dir, selected_inp)
