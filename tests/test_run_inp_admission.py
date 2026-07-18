import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orca_auto.core.admission import (
    AdmissionSlot,
    active_slot_count,
    list_slots,
    reserve_slot,
)
from orca_auto.orca.admission_env import (
    ADMISSION_APP_NAME_ENV_VAR,
    ADMISSION_TASK_ID_ENV_VAR,
    ADMISSION_TOKEN_ENV_VAR,
)
from orca_auto.orca.config import AppConfig, PathsConfig, RetryRuntimeConfig
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.state import load_state, state_path


def _make_cfg(tmp: str) -> AppConfig:
    root = Path(tmp)
    fake_orca = root / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = AppConfig(
        runtime=RetryRuntimeConfig(allowed_root=tmp),
        paths=PathsConfig(orca_executable=str(fake_orca)),
    )
    cfg.runtime.max_concurrent = 1
    return cfg


def _write_inp(reaction_dir: Path) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )


def _make_args(root: Path, reaction_dir: Path, **overrides) -> SimpleNamespace:
    defaults = {
        "config": str(root / "orca_auto.yaml"),
        "reaction_dir": str(reaction_dir),
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestRunInpAdmission(unittest.TestCase):
    @patch("orca_auto.orca.execution.load_config")
    @patch("orca_auto.orca.execution.run_attempts", return_value=0)
    def test_internal_run_rejects_without_queue_reservation(
        self,
        mock_run_attempts: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            rc = execute_orca_run(_make_args(root, reaction_dir))

            self.assertEqual(rc, 1)
            self.assertFalse(mock_run_attempts.called)
            self.assertEqual(active_slot_count(root), 0)
            self.assertFalse(state_path(reaction_dir).exists())
            self.assertFalse((reaction_dir / "run.lock").exists())

    @patch("orca_auto.orca.execution.load_config")
    def test_internal_run_holds_slot_during_execution_and_releases_after(
        self, mock_load_config: MagicMock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            observed_counts: list[int] = []

            def _fake_run_attempts(*args, **kwargs) -> int:
                observed_counts.append(active_slot_count(root))
                return 0

            token = reserve_slot(
                root, 1, queue_id="q_test", source="queue_worker", state="reserved"
            )
            self.assertIsNotNone(token)

            with (
                patch("orca_auto.orca.execution.run_attempts", new=_fake_run_attempts),
                patch.dict(
                    os.environ,
                    {ADMISSION_TOKEN_ENV_VAR: token or ""},
                    clear=False,
                ),
            ):
                rc = execute_orca_run(_make_args(root, reaction_dir))

            self.assertEqual(rc, 0)
            self.assertEqual(observed_counts, [1])
            self.assertEqual(active_slot_count(root), 0)

    @patch("orca_auto.orca.execution.load_config")
    @patch("orca_auto.orca.execution.run_attempts", return_value=0)
    def test_reserved_slot_from_queue_is_activated_and_released(
        self,
        mock_run_attempts: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            token = reserve_slot(
                root, 1, queue_id="q_test", source="queue_worker", state="reserved"
            )
            self.assertIsNotNone(token)

            with patch.dict(os.environ, {ADMISSION_TOKEN_ENV_VAR: token or ""}, clear=False):
                rc = execute_orca_run(_make_args(root, reaction_dir))

            self.assertEqual(rc, 0)
            self.assertTrue(mock_run_attempts.called)
            self.assertEqual(active_slot_count(root), 0)

    @patch("orca_auto.orca.execution.load_config")
    def test_reserved_slot_activation_attaches_task_metadata_from_worker_env(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn_meta"
            _write_inp(reaction_dir)

            token = reserve_slot(
                root, 1, queue_id="q_meta", source="queue_worker", state="reserved"
            )
            self.assertIsNotNone(token)
            observed_slots: list[AdmissionSlot] = []

            def _fake_run_attempts(*args, **kwargs) -> int:
                slots = list_slots(root)
                self.assertEqual(len(slots), 1)
                observed_slots.append(slots[0])
                return 0

            with (
                patch("orca_auto.orca.execution.run_attempts", new=_fake_run_attempts),
                patch.dict(
                    os.environ,
                    {
                        ADMISSION_TOKEN_ENV_VAR: token or "",
                        ADMISSION_APP_NAME_ENV_VAR: "orca_auto_orca",
                        ADMISSION_TASK_ID_ENV_VAR: "task_meta_456",
                    },
                    clear=False,
                ),
            ):
                rc = execute_orca_run(_make_args(root, reaction_dir))

            self.assertEqual(rc, 0)
            self.assertEqual(len(observed_slots), 1)
            self.assertEqual(observed_slots[0].app_name, "orca_auto_orca")
            self.assertEqual(observed_slots[0].task_id, "task_meta_456")
            self.assertEqual(observed_slots[0].work_dir, str(reaction_dir))
            self.assertEqual(active_slot_count(root), 0)
            state = load_state(reaction_dir)
            assert state is not None
            self.assertEqual(state["job_id"], "task_meta_456")

    @patch("orca_auto.orca.execution.load_config")
    def test_reserved_slot_is_released_when_existing_completed_out_skips_execution(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn_skip"
            _write_inp(reaction_dir)
            (reaction_dir / "rxn.out").write_text(
                "****ORCA TERMINATED NORMALLY****\n", encoding="utf-8"
            )

            token = reserve_slot(
                root, 1, queue_id="q_skip", source="queue_worker", state="reserved"
            )
            self.assertIsNotNone(token)

            with patch.dict(os.environ, {ADMISSION_TOKEN_ENV_VAR: token or ""}, clear=False):
                rc = execute_orca_run(_make_args(root, reaction_dir))

            self.assertEqual(rc, 0)
            self.assertEqual(active_slot_count(root), 0)
            state = load_state(reaction_dir)
            assert state is not None
            final_result = state["final_result"]
            assert final_result is not None
            self.assertEqual(final_result["reason"], "existing_out_completed")

    @patch("orca_auto.orca.execution.load_config")
    @patch("orca_auto.orca.execution.run_attempts", side_effect=RuntimeError("boom"))
    def test_reserved_slot_is_released_when_execution_raises_runtime_error(
        self,
        _mock_run_attempts: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn_error"
            _write_inp(reaction_dir)

            token = reserve_slot(
                root, 1, queue_id="q_error", source="queue_worker", state="reserved"
            )
            self.assertIsNotNone(token)

            with patch.dict(os.environ, {ADMISSION_TOKEN_ENV_VAR: token or ""}, clear=False):
                rc = execute_orca_run(_make_args(root, reaction_dir))

            self.assertEqual(rc, 1)
            self.assertEqual(active_slot_count(root), 0)
