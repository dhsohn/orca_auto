from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.orca.attempt.resume import (
    recover_missing_retry_input,
    resolve_execution_input,
    resume_terminal_decision,
)
from orca_auto.orca.state import new_state, state_path
from orca_auto.orca.types import RunState


class TestAttemptResume(unittest.TestCase):
    def test_recover_missing_retry_input_uses_selected_input_when_last_attempt_matches_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            current_inp = reaction_dir / "rxn.retry01.inp"
            selected_inp.write_text("! Opt\n", encoding="utf-8")
            current_inp.write_text("! Retry\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=2)
            state["attempts"].append(
                {
                    "inp_path": str(current_inp),
                    "patch_actions": [],
                }
            )

            with patch(
                "orca_auto.orca.attempt.resume.rewrite_for_retry",
                return_value=["route_add_tightscf_slowconv"],
            ):
                recovered, reason = recover_missing_retry_input(
                    reaction_dir=reaction_dir,
                    state=state,
                    selected_inp=selected_inp,
                    current_inp=current_inp,
                    retries_used=1,
                    to_resolved_local=lambda raw: Path(raw),
                    save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
                )

        self.assertTrue(recovered)
        self.assertEqual(reason, "resume_recovered")
        self.assertIn(
            "resume_recreated_missing_input:rxn.retry01.inp", state["attempts"][-1]["patch_actions"]
        )
        self.assertIn("resume_route_add_tightscf_slowconv", state["attempts"][-1]["patch_actions"])

    def test_resolve_execution_input_reports_missing_first_input_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = Path(td) / "missing.inp"
            state: RunState = {"attempts": []}

            current_inp, reason = resolve_execution_input(
                reaction_dir=reaction_dir,
                selected_inp=selected_inp,
                state=state,
                execution_index=1,
                retries_used=0,
                retry_inp_path=lambda inp, retry_number: inp.with_name(
                    f"{inp.stem}.retry{retry_number:02d}.inp"
                ),
                to_resolved_local=lambda raw: Path(raw),
                save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
            )

        self.assertIsNone(current_inp)
        self.assertEqual(reason, "missing_input_for_attempt_1")

    def test_resolve_execution_input_reports_no_output_when_recovery_does_not_create_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=2)
            state["attempts"].append({"inp_path": str(selected_inp), "patch_actions": []})

            with patch("orca_auto.orca.attempt.resume.rewrite_for_retry", return_value=[]):
                current_inp, reason = resolve_execution_input(
                    reaction_dir=reaction_dir,
                    selected_inp=selected_inp,
                    state=state,
                    execution_index=2,
                    retries_used=1,
                    retry_inp_path=lambda inp, retry_number: inp.with_name(
                        f"{inp.stem}.retry{retry_number:02d}.inp"
                    ),
                    to_resolved_local=lambda raw: Path(raw),
                    save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
                )

        self.assertIsNone(current_inp)
        assert reason is not None
        self.assertEqual(reason, "missing_input_for_attempt_2:resume_recovery_no_output")

    def test_resume_terminal_decision_returns_terminal_result_for_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=2)
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
                max_retries=2,
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
