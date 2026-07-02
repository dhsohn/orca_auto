from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orca_auto.orca.commands.organize_notifications import _build_organize_message
from orca_auto.orca.commands.organize_output import build_dry_run_summary
from orca_auto.orca.commands.organize_service import resolve_organize_scope
from orca_auto.orca.config import AppConfig, PathsConfig, RuntimeConfig, TelegramConfig
from orca_auto.orca.result_organizer.models import OrganizePlan, SkipReason


def _make_cfg(tmp: str) -> AppConfig:
    return AppConfig(
        runtime=RuntimeConfig(
            allowed_root=tmp,
            organized_root=str(Path(tmp) / "organized"),
        ),
        paths=PathsConfig(),
        telegram=TelegramConfig(),
    )


def _plan(root: Path) -> OrganizePlan:
    source_dir = root / "runs" / "rxn1"
    target_abs_path = root / "organized" / "opt" / "H2" / "run_1"
    return OrganizePlan(
        reaction_dir=source_dir,
        run_id="run_1",
        job_type="opt",
        molecule_key="H2",
        selected_inp=str(source_dir / "rxn.inp"),
        last_out_path=str(source_dir / "rxn.out"),
        attempt_count=1,
        status="completed",
        analyzer_status="completed",
        reason="normal_termination",
        completed_at="2026-03-22T00:00:00+00:00",
        source_dir=source_dir,
        target_rel_path="opt/H2/run_1",
        target_abs_path=target_abs_path,
    )


class TestOrganizeCommandHelpers(unittest.TestCase):
    def test_build_dry_run_summary_serializes_plans_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            summary = build_dry_run_summary(
                [_plan(root)],
                [SkipReason("rxn_skip", "not_completed")],
            )

        self.assertEqual(summary["action"], "dry_run")
        self.assertEqual(summary["to_organize"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["plans"][0]["run_id"], "run_1")
        self.assertEqual(summary["skip_reasons"][0]["reason"], "not_completed")

    def test_build_organize_message_returns_none_when_empty(self) -> None:
        self.assertIsNone(_build_organize_message([], [], [], []))

    def test_build_organize_message_includes_summary_sections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            message = _build_organize_message(
                [{"run_id": "run_1", "_plan": _plan(root), "action": "moved"}],
                [{"run_id": "run_2", "action": "skipped", "reason": "already_organized"}],
                [{"run_id": "run_3", "reason": "apply_failed"}],
                [SkipReason("rxn_skip", "not_completed")],
            )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("Summary", message)
        self.assertIn("Organized", message)
        self.assertIn("Failed", message)
        self.assertIn("Skipped", message)

    def test_resolve_organize_scope_rejects_invalid_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_cfg(td)
            result = resolve_organize_scope(
                cfg,
                organized_root=Path(cfg.runtime.organized_root),
                reaction_dir_raw=None,
                root_raw=str(Path(td) / "missing"),
            )

        self.assertIsNone(result)
