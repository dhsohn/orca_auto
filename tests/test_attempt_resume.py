from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orca_auto.orca.attempt.resume import (
    resume_terminal_decision,
)
from orca_auto.orca.state import new_state


class TestAttemptResume(unittest.TestCase):
    def test_resume_terminal_decision_returns_terminal_result_for_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)
            state["attempts"].append(
                {
                    "analyzer_status": "completed",
                    "analyzer_reason": "normal_termination",
                    "out_path": str(reaction_dir / "rxn.out"),
                }
            )
            exit_calls: list[dict] = []

            def _exit_with_result(*args, **kwargs) -> int:
                del args
                exit_calls.append(kwargs)
                return 0

            result = resume_terminal_decision(
                reaction_dir=reaction_dir,
                selected_inp=selected_inp,
                state=state,
                resumed=True,
                last_out_path_from_state=lambda current_state: current_state["attempts"][-1].get(
                    "out_path"
                ),
                exit_with_result=_exit_with_result,
                emit=lambda _payload: None,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(exit_calls), 1)
        self.assertEqual(exit_calls[0]["reason"], "normal_termination")
        self.assertEqual(exit_calls[0]["status"].value, "completed")
