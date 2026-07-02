from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.orca.commands.organize_notifications import _build_organize_message
from orca_auto.orca.commands.organize_service import (
    cmd_organize_apply,
    organize_reaction_dir,
)
from orca_auto.orca.commands.organize_tracking import build_index_record
from orca_auto.orca.config import AppConfig, PathsConfig, RuntimeConfig
from orca_auto.orca.result_organizer.models import OrganizePlan, SkipReason


def _make_plan(root: Path, name: str = "rxn1") -> OrganizePlan:
    source_dir = root / "runs" / name
    target_abs_path = root / "organized" / "opt" / name
    return OrganizePlan(
        reaction_dir=source_dir,
        run_id=f"run_{name}",
        job_type="opt",
        molecule_key=f"mol_{name}",
        selected_inp=str(source_dir / "rxn.inp"),
        last_out_path=str(source_dir / "rxn.out"),
        attempt_count=1,
        status="completed",
        analyzer_status="completed",
        reason="normal_termination",
        completed_at="2026-03-22T00:00:00+00:00",
        source_dir=source_dir,
        target_rel_path=f"opt/{name}",
        target_abs_path=target_abs_path,
    )


class TestOrganizeHelpers(unittest.TestCase):
    def test_build_index_record_normalizes_relative_paths_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = _make_plan(root)
            target_dir = plan.target_abs_path
            target_dir.mkdir(parents=True)
            state = {
                "status": "completed",
                "selected_inp": str(target_dir / "rxn.inp"),
                "attempts": "invalid",
                "final_result": {
                    "last_out_path": str(target_dir / "rxn.out"),
                },
            }

            record = build_index_record(plan, state)

        self.assertEqual(record["reaction_dir"], str(plan.target_abs_path))
        self.assertEqual(record["selected_inp"], "rxn.inp")
        self.assertEqual(record["last_out_path"], "rxn.out")
        self.assertEqual(record["attempt_count"], 0)
        self.assertEqual(record["analyzer_status"], "")
        self.assertEqual(record["reason"], "")

    def test_build_organize_message_returns_none_when_no_results(self) -> None:
        message = _build_organize_message([], [], [], [])

        self.assertIsNone(message)

    def test_build_organize_message_summarizes_organized_failed_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = _make_plan(Path(td), name="example_run")
            message = _build_organize_message(
                organized=[{"run_id": plan.run_id, "_plan": plan}],
                skipped=[],
                failures=[{"run_id": "run_failed", "reason": "conflict"}],
                skips=[SkipReason("rxn_skip", "already_organized")],
            )

        assert message is not None
        self.assertIn("Organized: 1", message)
        self.assertIn("Failed: 1", message)
        self.assertIn("Skipped: 1", message)
        self.assertIn(plan.target_rel_path, message)
        self.assertIn("run_failed", message)
        self.assertIn("already_organized", message)

    def test_cmd_organize_apply_treats_already_organized_as_skip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = _make_plan(root)
            cfg = AppConfig(
                runtime=RuntimeConfig(
                    allowed_root=str(root / "runs"), organized_root=str(root / "organized")
                ),
                paths=PathsConfig(orca_executable="/usr/bin/true"),
            )
            captured = {}

            def _finalize(summary, emit_fn, failures):
                captured["summary"] = summary
                captured["failures"] = failures
                emit_fn(summary)
                return 1 if failures else 0

            with (
                patch(
                    "orca_auto.orca.commands.organize_service.acquire_index_lock",
                    return_value=contextlib.nullcontext(),
                ),
                patch(
                    "orca_auto.orca.commands.organize_service.load_index",
                    return_value=[],
                ),
                patch(
                    "orca_auto.orca.commands.organize_service.check_conflict",
                    return_value="already_organized",
                ),
                patch(
                    "orca_auto.orca.commands.organize_service.finalize_batch_apply",
                    side_effect=_finalize,
                ),
                patch(
                    "orca_auto.orca.commands.organize_output.emit_organize",
                    return_value=None,
                ) as emit_mock,
                patch(
                    "orca_auto.orca.commands.organize_service.execute_move",
                ) as move_mock,
            ):
                rc = cmd_organize_apply([plan], [], root / "organized", cfg)

        self.assertEqual(rc, 0)
        self.assertEqual(captured["summary"]["organized"], 0)
        self.assertEqual(captured["summary"]["skipped"], 1)
        self.assertEqual(captured["summary"]["failed"], 0)
        self.assertEqual(captured["failures"], [])
        move_mock.assert_not_called()
        emit_mock.assert_called_once()

    @patch("orca_auto.orca.commands.organize_service.apply_organize_plans")
    @patch("orca_auto.orca.commands.organize_service.resolve_organize_scope")
    def test_organize_reaction_dir_returns_organized_payload(
        self,
        mock_scope: unittest.mock.MagicMock,
        mock_apply: unittest.mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AppConfig(
                runtime=RuntimeConfig(
                    allowed_root=str(root / "runs"), organized_root=str(root / "organized")
                ),
                paths=PathsConfig(orca_executable="/usr/bin/true"),
            )
            plan = _make_plan(root)
            mock_scope.return_value = ([plan], [])
            mock_apply.return_value = {
                "organized": 1,
                "skipped": 0,
                "failed": 0,
                "failures": [],
                "_organized_results": [{"_plan": plan}],
                "_skipped_results": [],
                "_skip_reasons": [],
            }

            result = organize_reaction_dir(cfg, plan.source_dir, notify_summary=False)

        self.assertEqual(result["action"], "organized")
        self.assertEqual(result["run_id"], plan.run_id)
        self.assertEqual(result["target_dir"], str(plan.target_abs_path))

    @patch("orca_auto.orca.commands.organize_service.resolve_organize_scope")
    def test_organize_reaction_dir_returns_skip_reason_for_empty_scope(
        self,
        mock_scope: unittest.mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AppConfig(
                runtime=RuntimeConfig(
                    allowed_root=str(root / "runs"), organized_root=str(root / "organized")
                ),
                paths=PathsConfig(orca_executable="/usr/bin/true"),
            )
            mock_scope.return_value = ([], [SkipReason("rxn_skip", "already_organized")])

            result = organize_reaction_dir(cfg, root / "runs" / "rxn_skip", notify_summary=False)

        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["reason"], "already_organized")

    @patch("orca_auto.orca.commands.organize_service.resolve_organize_scope")
    def test_organize_reaction_dir_uses_workflow_local_organized_root(
        self,
        mock_scope: unittest.mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workflow_root = root / "workflow_root"
            reaction_dir = workflow_root / "wf_local" / "03_orca" / "job_01"
            expected_organized_root = workflow_root / "wf_local" / "03_orca"
            cfg = AppConfig(
                runtime=RuntimeConfig(
                    allowed_root=str(root / "runs"), organized_root=str(root / "organized")
                ),
                workflow_root=str(workflow_root),
                paths=PathsConfig(orca_executable="/usr/bin/true"),
            )
            mock_scope.return_value = ([], [])

            organize_reaction_dir(cfg, reaction_dir, notify_summary=False)

        self.assertEqual(
            mock_scope.call_args.kwargs["organized_root"],
            expected_organized_root.resolve(),
        )
