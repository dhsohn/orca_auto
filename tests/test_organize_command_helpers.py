from __future__ import annotations

import unittest
from pathlib import Path

from orca_auto.orca.commands.organize_notifications import (
    _ORGANIZE_RESULT_LIMIT,
    _build_organize_message,
)
from orca_auto.orca.commands.organize_tracking import build_index_record
from orca_auto.orca.result_organizer.models import OrganizePlan, SkipReason


def _plan(*, run_id: str = "run_1234567890ab") -> OrganizePlan:
    source_dir = Path("/tmp/runs/rxn1")
    target_abs = Path("/tmp/organized/rxn1")
    return OrganizePlan(
        reaction_dir=source_dir,
        run_id=run_id,
        job_type="opt",
        molecule_key="H2",
        selected_inp=str(source_dir / "rxn.inp"),
        last_out_path=str(source_dir / "rxn.out"),
        attempt_count=2,
        status="completed",
        analyzer_status="completed",
        reason="normal_termination",
        completed_at="2026-03-22T00:00:00+00:00",
        source_dir=source_dir,
        target_rel_path="opt/H2/rxn1",
        target_abs_path=target_abs,
    )


class TestOrganizeCommandHelpers(unittest.TestCase):
    def test_build_index_record_relativizes_artifacts_under_target(self) -> None:
        plan = _plan()
        state = {
            "status": "completed",
            "selected_inp": str(plan.target_abs_path / "rxn.inp"),
            "attempts": [{"index": 1}, {"index": 2}],
            "final_result": {
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-03-22T00:00:00+00:00",
                "last_out_path": str(plan.target_abs_path / "rxn.out"),
            },
        }

        record = build_index_record(plan, state)

        self.assertEqual(record["run_id"], plan.run_id)
        self.assertEqual(record["reaction_dir"], str(plan.target_abs_path))
        self.assertEqual(record["selected_inp"], "rxn.inp")
        self.assertEqual(record["last_out_path"], "rxn.out")
        self.assertEqual(record["attempt_count"], 2)
        self.assertEqual(record["organized_path"], plan.target_rel_path)

    def test_build_organize_message_returns_none_when_empty(self) -> None:
        message = _build_organize_message([], [], [], [])
        self.assertIsNone(message)

    def test_build_organize_message_includes_summary_and_limits(self) -> None:
        organized = [
            {
                "run_id": f"run_{idx:02d}",
                "action": "moved",
                "_plan": _plan(run_id=f"run_{idx:02d}_abcdef123456"),
            }
            for idx in range(_ORGANIZE_RESULT_LIMIT + 1)
        ]
        skipped = [{"run_id": "run_skip", "action": "skipped", "reason": "already_organized"}]
        failures = [{"run_id": "run_fail", "reason": "index write failed"}]
        skip_reasons = [SkipReason(f"rxn_{idx}", "not_completed") for idx in range(6)]

        message = _build_organize_message(organized, skipped, failures, skip_reasons)

        assert message is not None
        self.assertIn("orca_auto organize", message)
        self.assertIn("Summary", message)
        self.assertIn("Organized", message)
        self.assertIn("Failed", message)
        self.assertIn("Skipped", message)
        self.assertIn("showing 10/11", message)
        self.assertIn("showing 5/7", message)
        self.assertIn("index write failed", message)
        self.assertIn("not_completed", message)
