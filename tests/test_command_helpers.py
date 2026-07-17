from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import orca_auto.orca.commands._helpers as command_helpers
from orca_auto.orca.commands._helpers import (
    CONFIG_ENV_VAR,
    _emit,
    _human_bytes,
    _validate_reaction_dir,
    _validate_root_scan_dir,
    default_config_path,
    finalize_batch_apply,
)
from orca_auto.orca.config import AppConfig, PathsConfig, RetryRuntimeConfig


def _cfg(allowed_root: Path, *, workflow_root: Path | None = None) -> AppConfig:
    return AppConfig(
        runtime=RetryRuntimeConfig(
            allowed_root=str(allowed_root),
            default_max_retries=3,
        ),
        workflow_root=str(workflow_root.resolve()) if workflow_root is not None else "",
        paths=PathsConfig(orca_executable="/usr/bin/orca"),
    )


class TestCommandPathValidators(unittest.TestCase):
    def test_validate_reaction_dir_under_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            reaction = allowed / "r1"
            allowed.mkdir()
            reaction.mkdir()
            cfg = _cfg(allowed)

            resolved = _validate_reaction_dir(cfg, str(reaction))
            self.assertEqual(resolved, reaction.resolve())

    def test_validate_reaction_dir_rejects_outside_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            cfg = _cfg(allowed)

            with self.assertRaises(ValueError):
                _validate_reaction_dir(cfg, str(outside))

    def test_validate_reaction_dir_requires_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            allowed.mkdir()
            cfg = _cfg(allowed)

            with self.assertRaises(ValueError):
                _validate_reaction_dir(cfg, str(allowed / "missing"))

    def test_validate_reaction_dir_accepts_workflow_local_orca_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workflow_root = root / "workflow_root"
            allowed = root / "allowed"
            reaction = workflow_root / "wf_example" / "03_orca" / "job_01"
            allowed.mkdir()
            reaction.mkdir(parents=True)
            cfg = _cfg(allowed, workflow_root=workflow_root)

            resolved = _validate_reaction_dir(cfg, str(reaction))
            self.assertEqual(resolved, reaction.resolve())

    def test_root_validators_require_exact_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            allowed.mkdir()
            cfg = _cfg(allowed)

            with self.subTest("allowed_root_exact"):
                self.assertEqual(_validate_root_scan_dir(cfg, str(allowed)), allowed.resolve())
            with self.subTest("allowed_root_mismatch"):
                with self.assertRaises(ValueError):
                    _validate_root_scan_dir(cfg, str(allowed / "nested"))

    def test_validate_root_scan_dir_requires_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            allowed.mkdir()
            cfg = _cfg(allowed)

            with self.assertRaises(ValueError):
                _validate_root_scan_dir(cfg, str(root / "missing"))


class TestHelperUtilities(unittest.TestCase):
    def test_default_config_path_prefers_primary_repo_then_home_then_repo_default(self) -> None:
        repo_root = Path(command_helpers.__file__).resolve().parents[4]
        repo_default = repo_root / "config" / "orca_auto.yaml"
        original_exists = Path.exists

        with patch.dict(os.environ, {CONFIG_ENV_VAR: ""}, clear=False):

            def repo_exists(path: Path) -> bool:
                if path == repo_default:
                    return True
                return original_exists(path)

            with patch.object(Path, "exists", repo_exists):
                self.assertEqual(default_config_path(), str(repo_default))

            fake_home = repo_root / "tmp_home_for_test"
            home_default = fake_home / "orca_auto" / "config" / "orca_auto.yaml"

            def home_exists(path: Path) -> bool:
                if path == repo_default:
                    return False
                if path == home_default:
                    return True
                return original_exists(path)

            with (
                patch.object(Path, "home", return_value=fake_home),
                patch.object(
                    Path,
                    "exists",
                    home_exists,
                ),
            ):
                self.assertEqual(default_config_path(), str(home_default))

            def fallback_exists(path: Path) -> bool:
                if path in {repo_default, home_default}:
                    return False
                return original_exists(path)

            with (
                patch.object(Path, "home", return_value=fake_home),
                patch.object(
                    Path,
                    "exists",
                    fallback_exists,
                ),
            ):
                self.assertEqual(default_config_path(), str(repo_default))

    def test_human_bytes_formats_values(self) -> None:
        self.assertEqual(_human_bytes(1), "1.0 B")
        self.assertEqual(_human_bytes(1536), "1.5 KB")
        self.assertEqual(_human_bytes(1024**2), "1.0 MB")
        self.assertEqual(_human_bytes(1024**4), "1.0 TB")

    def test_emit_prints_only_known_keys(self) -> None:
        payload = {
            "status": "completed",
            "reaction_dir": "/tmp/rxn",
            "selected_inp": "rxn.inp",
            "attempt_count": 2,
            "reason": "normal_termination",
            "report_md": "/tmp/report.md",
            "ignored": "value",
        }

        captured = io.StringIO()
        with redirect_stdout(captured):
            _emit(payload)

        output = captured.getvalue()
        self.assertIn("status: completed", output)
        self.assertIn("job_dir: /tmp/rxn", output)
        self.assertIn("report_md: /tmp/report.md", output)
        self.assertNotIn("ignored", output)


class TestFinalizeBatchApply(unittest.TestCase):
    def test_returns_zero_when_no_failures(self) -> None:
        emitted = []

        def _emit(payload):
            emitted.append(payload)

        rc = finalize_batch_apply(
            {"action": "apply", "failed": 0},
            _emit,
            [],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(emitted, [{"action": "apply", "failed": 0}])

    def test_returns_one_when_failures_exist(self) -> None:
        rc = finalize_batch_apply(
            {"action": "apply", "failed": 1},
            lambda _payload: None,
            [{"run_id": "run_001", "reason": "apply_failed"}],
        )
        self.assertEqual(rc, 1)
