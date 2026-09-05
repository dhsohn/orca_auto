from __future__ import annotations

from pathlib import Path
from typing import cast

from orca_auto.orca.attempt import resume as attempt_resume
from orca_auto.orca.state import new_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import RunFinishedNotification, RunState


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

    def notify_finished(payload: RunFinishedNotification) -> None:
        del payload

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
        notify_finished=notify_finished,
    )

    assert result == 7
    assert len(exit_calls) == 1
    assert exit_calls[0]["status"] == RunStatus.FAILED
    assert exit_calls[0]["analyzer_status"] == AnalyzerStatus.INCOMPLETE.value
    assert exit_calls[0]["reason"] == "resume_last_attempt"
    assert exit_calls[0]["last_out_path"] == "state.out"
    assert exit_calls[0]["notify_finished"] is notify_finished
