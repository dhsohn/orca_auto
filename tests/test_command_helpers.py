from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR as CONFIG_ENV_VAR
from orca_auto.core.config.discovery import default_shared_config_path as default_config_path
from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig, PathsConfig
from orca_auto.orca.execution import _emit
from orca_auto.orca.run_context import _validate_reaction_dir


def _cfg(allowed_root: Path) -> AppConfig:
    return AppConfig(
        runtime=OrcaRuntimeConfig(
            allowed_root=str(allowed_root),
        ),
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


class TestHelperUtilities(unittest.TestCase):
    def test_default_config_path_prefers_env_then_home_default(self) -> None:
        fake_home = Path(tempfile.gettempdir()) / "tmp_home_for_test"
        home_default = fake_home / "orca_auto" / "config" / "orca_auto.yaml"

        with (
            patch.dict(os.environ, {CONFIG_ENV_VAR: ""}, clear=False),
            patch.object(Path, "home", return_value=fake_home),
        ):
            # The home default is the only implicit location, whether or not
            # the file exists yet; no checkout-relative path is probed.
            self.assertEqual(default_config_path(), str(home_default))

        with patch.dict(os.environ, {CONFIG_ENV_VAR: "/tmp/env.yaml"}, clear=False):
            self.assertEqual(default_config_path(), "/tmp/env.yaml")

    def test_emit_prints_only_known_keys(self) -> None:
        payload = {
            "status": "completed",
            "reaction_dir": "/tmp/rxn",
            "selected_inp": "rxn.inp",
            "attempt_count": 2,
            "reason": "normal_termination",
            "report_json": "/tmp/report.json",
            "ignored": "value",
        }

        captured = io.StringIO()
        with redirect_stdout(captured):
            _emit(payload)

        output = captured.getvalue()
        self.assertIn("status: completed", output)
        self.assertIn("job_dir: /tmp/rxn", output)
        self.assertIn("report_json: /tmp/report.json", output)
        self.assertNotIn("ignored", output)
