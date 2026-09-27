from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from orca_auto.orca.attempt import resume as attempt_resume
from orca_auto.orca.attempt.resume import resume_terminal_decision
from orca_auto.orca.state import new_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import RunState


def test_resume_terminal_decision_returns_terminal_result_for_completed_attempt(
    tmp_path: Path,
) -> None:
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
    exit_calls: list[dict[str, Any]] = []

    def _exit_with_result(*args: Any, **kwargs: Any) -> int:
        del args
        exit_calls.append(kwargs)
        return 0

    result = resume_terminal_decision(
        reaction_dir=tmp_path,
        selected_inp=selected_inp,
        state=state,
        resumed=True,
        last_out_path_from_state=lambda current_state: current_state["attempts"][-1].get(
            "out_path"
        ),
        exit_with_result=_exit_with_result,
        emit=lambda _payload: None,
    )

    assert result == 0
    assert len(exit_calls) == 1
    assert exit_calls[0]["reason"] == "normal_termination"
    assert exit_calls[0]["status"].value == "completed"


def test_attempt_resume_text_and_patch_action_helpers_cover_existing_and_missing_values() -> None:
    assert attempt_resume._as_non_empty_text(" hello ") == "hello"
    assert attempt_resume._as_non_empty_text("   ") is None
    assert attempt_resume._as_non_empty_text(123) is None


def test_resume_terminal_decision_covers_non_resumed_malformed_and_defaulted_terminal_paths(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    state = new_state(reaction_dir, selected_inp)
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=state,
            resumed=False,
            last_out_path_from_state=lambda current_state: current_state.get("selected_inp"),
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=cast(RunState, {"attempts": "bad"}),
            resumed=True,
            last_out_path_from_state=lambda current_state: None,
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=cast(RunState, {"attempts": ["bad"]}),
            resumed=True,
            last_out_path_from_state=lambda current_state: None,
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )

    recorded_incomplete_state: RunState = {
        "attempts": [
            {
                "analyzer_status": AnalyzerStatus.INCOMPLETE.value,
                "analyzer_reason": "still_running",
            }
        ]
    }
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=recorded_incomplete_state,
            resumed=True,
            last_out_path_from_state=lambda current_state: None,
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        == 0
    )

    terminal_state: RunState = {
        "attempts": [
            {"analyzer_status": "completed", "analyzer_reason": "normal_termination"},
            {"analyzer_status": " ", "analyzer_reason": " ", "out_path": " "},
        ]
    }
    exit_calls: list[dict[str, object]] = []

    def _exit_with_result(*args: object, **kwargs: object) -> int:
        del args
        exit_calls.append(dict(kwargs))
        return 7

    result = attempt_resume.resume_terminal_decision(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        state=terminal_state,
        resumed=True,
        last_out_path_from_state=lambda current_state: "state.out",
        exit_with_result=_exit_with_result,
        emit=lambda _payload: None,
    )

    assert result == 7
    assert len(exit_calls) == 1
    assert exit_calls[0]["status"] == RunStatus.FAILED
    assert exit_calls[0]["analyzer_status"] == AnalyzerStatus.INCOMPLETE.value
    assert exit_calls[0]["reason"] == "resume_last_attempt"
    assert exit_calls[0]["last_out_path"] == "state.out"
