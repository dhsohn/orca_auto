from __future__ import annotations

import unittest
from pathlib import Path

from orca_auto.orca.commands.organize_notifications import _build_organize_message
from orca_auto.orca.commands.organize_output import build_dry_run_summary
from orca_auto.orca.result_organizer.models import OrganizePlan, SkipReason


def _sample_plan() -> OrganizePlan:
    return OrganizePlan(
        reaction_dir=Path("/tmp/source/run_a"),
        run_id="run_20260322_abcdef12",
        job_type="opt",
        molecule_key="H2",
        selected_inp="rxn.inp",
        last_out_path="rxn.out",
        attempt_count=1,
        status="completed",
        analyzer_status="completed",
        reason="normal_termination",
        completed_at="2026-03-22T00:00:00+00:00",
        source_dir=Path("/tmp/source/run_a"),
        target_abs_path=Path("/tmp/organized/opt/h2/run_a"),
        target_rel_path="opt/h2/run_a",
    )


class TestOrganizeMessage(unittest.TestCase):
    def test_build_dry_run_summary_serializes_plans_and_skips(self) -> None:
        plan = _sample_plan()
        skip = SkipReason(reaction_dir="runs/rxn_skip", reason="status_not_completed")

        summary = build_dry_run_summary([plan], [skip])

        self.assertEqual(summary["action"], "dry_run")
        self.assertEqual(summary["to_organize"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["plans"][0]["run_id"], plan.run_id)
        self.assertEqual(summary["skip_reasons"][0]["reaction_dir"], "runs/rxn_skip")

    def test_build_organize_message_returns_none_when_empty(self) -> None:
        self.assertIsNone(_build_organize_message([], [], [], []))

    def test_build_organize_message_includes_summary_and_sections(self) -> None:
        plan = _sample_plan()
        organized = [{"run_id": plan.run_id, "action": "moved", "_plan": plan}]
        skipped = [{"run_id": "run_skip", "action": "skipped", "reason": "already_organized"}]
        failures = [{"run_id": "run_fail", "reason": "apply_failed: boom"}]
        skips = [SkipReason(reaction_dir="runs/rxn_skip", reason="status_not_completed")]

        message = _build_organize_message(organized, skipped, failures, skips)

        assert message is not None
        self.assertIn("orca_auto organize", message)
        self.assertIn("Organized: 1", message)
        self.assertIn("Skipped: 2", message)
        self.assertIn("Failed: 1", message)
        self.assertIn(plan.target_rel_path, message)
        self.assertIn("apply_failed: boom", message)
        self.assertIn("runs/rxn_skip", message)
