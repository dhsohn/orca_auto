import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_auto.core.admission import active_slot_count
from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig, PathsConfig
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.run_context import RunExecutionContext, configured_admission_root
from orca_auto.orca.state_reading import state_path


def _make_cfg(tmp: str) -> AppConfig:
    root = Path(tmp)
    fake_orca = root / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = AppConfig(
        runtime=OrcaRuntimeConfig(allowed_root=tmp, max_concurrent=1),
        paths=PathsConfig(orca_executable=str(fake_orca)),
    )
    return cfg


def _write_inp(reaction_dir: Path) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )


class TestRunInpAdmission(unittest.TestCase):
    @patch("orca_auto.orca.execution.run_attempts", return_value=0)
    def test_internal_run_rejects_without_queue_reservation(
        self,
        mock_run_attempts: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            rc = execute_orca_run(
                RunExecutionContext(
                    cfg=cfg,
                    reaction_dir=reaction_dir,
                    selected_inp=reaction_dir / "rxn.inp",
                    admission_root=configured_admission_root(cfg),
                )
            )

            self.assertEqual(rc, 1)
            self.assertFalse(mock_run_attempts.called)
            self.assertEqual(active_slot_count(root), 0)
            self.assertFalse(state_path(reaction_dir).exists())
            # The advisory lock file persists; only kernel ownership is released.
            self.assertTrue((reaction_dir / "run.lock").exists())
