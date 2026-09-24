from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.orca.attempt.resume import resume_terminal_decision
from orca_auto.orca.state import new_state


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
