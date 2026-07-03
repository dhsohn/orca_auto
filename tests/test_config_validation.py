import json
import tempfile
import unittest
from pathlib import Path

from orca_auto.orca.config import load_config


def _orca_config(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    existing_orca = normalized.pop("orca", {})
    orca = dict(existing_orca) if isinstance(existing_orca, dict) else {}
    for key in ("runtime", "paths"):
        value = normalized.pop(key, None)
        if value is not None:
            orca[key] = value
    normalized["orca"] = orca
    return normalized


def _write_fake_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_orca_config(config_path: Path, payload: dict[str, object]) -> Path:
    config_path.write_text(json.dumps(_orca_config(payload)), encoding="utf-8")
    return config_path


class TestConfigValidation(unittest.TestCase):
    def test_windows_allowed_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": "C:\\orca_runs",
                    },
                    "paths": {"orca_executable": "/opt/orca/orca"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("Linux path", str(ctx.exception))

    def test_windows_mount_allowed_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": "/mnt/c/orca_runs",
                    },
                    "paths": {"orca_executable": "/home/user/opt/orca/orca"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("Linux path", str(ctx.exception))

    def test_relative_paths_raise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": "./orca_runs",
                    },
                    "paths": {"orca_executable": "./opt/orca/orca"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("absolute Linux path", str(ctx.exception))

    def test_windows_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": "/home/user/orca_runs",
                    },
                    "paths": {"orca_executable": "C:\\Orca\\orca.exe"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("Linux path", str(ctx.exception))

    def test_exe_suffix_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": "/home/user/orca_runs",
                    },
                    "paths": {"orca_executable": "/home/user/opt/orca/orca.exe"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("Linux ORCA binary", str(ctx.exception))

    def test_linux_paths_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": str(allowed),
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.allowed_root, str(allowed))
            self.assertEqual(cfg.paths.orca_executable, str(fake_orca))

    def test_telegram_delivery_settings_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": str(allowed),
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                    "telegram": {
                        "bot_token": "token",
                        "chat_id": "chat",
                        "timeout_seconds": 3.5,
                        "max_attempts": 4,
                        "retry_backoff_seconds": 0.25,
                    },
                },
            )

            cfg = load_config(str(cfg_path))

            self.assertEqual(cfg.telegram.bot_token, "token")
            self.assertEqual(cfg.telegram.chat_id, "chat")
            self.assertEqual(cfg.telegram.timeout_seconds, 3.5)
            self.assertEqual(cfg.telegram.max_attempts, 4)
            self.assertEqual(cfg.telegram.retry_backoff_seconds, 0.25)

    def test_workflow_root_is_preserved_with_engine_scoped_orca_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workflow_root = root / "workflow_runs"
            workflow_root.mkdir()
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "workflow": {
                        "root": str(workflow_root),
                    },
                    "orca": {
                        "runtime": {
                            "allowed_root": str(allowed),
                        },
                        "paths": {"orca_executable": str(fake_orca)},
                    },
                },
            )

            cfg = load_config(str(cfg_path))

            self.assertEqual(cfg.workflow_root, str(workflow_root.resolve()))
            self.assertEqual(cfg.runtime.allowed_root, str(allowed.resolve()))
            self.assertEqual(cfg.paths.orca_executable, str(fake_orca.resolve()))

    def test_default_max_retries_can_exceed_five(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": str(allowed),
                        "default_max_retries": 9,
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.default_max_retries, 9)
            self.assertEqual(cfg.runtime.max_concurrent, 4)

    def test_resources_section_and_common_runtime_conversion_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "scheduler": {
                        "max_active_simulations": 6,
                    },
                    "runtime": {
                        "allowed_root": str(allowed),
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                    "resources": {
                        "max_cores_per_task": 12,
                        "max_memory_gb_per_task": 48,
                    },
                },
            )
            cfg = load_config(str(cfg_path))

            self.assertEqual(cfg.resources.max_cores_per_task, 12)
            self.assertEqual(cfg.resources.max_memory_gb_per_task, 48)

            self.assertEqual(cfg.runtime.allowed_root, str(allowed))
            self.assertEqual(cfg.runtime.max_concurrent, 6)
            self.assertEqual(cfg.runtime.resolved_admission_limit, 6)
            self.assertEqual(cfg.runtime.resolved_admission_root, str(root / "admission"))

    def test_orca_runtime_scheduler_keys_are_ignored_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": str(allowed),
                        "max_concurrent": 6,
                        "admission_root": str(root / "runtime-admission"),
                        "admission_limit": 3,
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )

            with self.assertNoLogs("orca_auto.orca.config", level="WARNING"):
                cfg = load_config(str(cfg_path))

            self.assertEqual(cfg.runtime.max_concurrent, 4)
            self.assertEqual(cfg.runtime.admission_root, str(allowed))
            self.assertIsNone(cfg.runtime.admission_limit)

    def test_scheduler_settings_ignore_orca_runtime_scheduler_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            scheduler_admission = root / "scheduler-admission"
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "scheduler": {
                        "max_active_simulations": 7,
                        "admission_root": str(scheduler_admission),
                    },
                    "runtime": {
                        "allowed_root": str(allowed),
                        "max_concurrent": 2,
                        "admission_root": str(root / "runtime-admission"),
                        "admission_limit": 2,
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )

            with self.assertNoLogs("orca_auto.orca.config", level="WARNING"):
                cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.max_concurrent, 7)
            self.assertEqual(cfg.runtime.admission_root, str(scheduler_admission))
            self.assertEqual(cfg.runtime.admission_limit, 7)

    def test_scheduler_max_active_simulations_rejects_invalid_explicit_values(self) -> None:
        for value in ("bad", 0, -1, True):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                allowed = root / "orca_runs"
                allowed.mkdir()
                fake_orca = root / "orca"
                _write_fake_executable(fake_orca)

                cfg_path = _write_orca_config(
                    root / "orca_auto.yaml",
                    {
                        "scheduler": {"max_active_simulations": value},
                        "runtime": {"allowed_root": str(allowed)},
                        "paths": {"orca_executable": str(fake_orca)},
                    },
                )

                with self.assertRaises(ValueError) as ctx:
                    load_config(str(cfg_path))
                self.assertIn(
                    "scheduler.max_active_simulations must be an integer >= 1",
                    str(ctx.exception),
                )

    def test_missing_config_file_raises_with_setup_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "orca_auto.yaml"
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("Config file not found", str(ctx.exception))
            self.assertIn("orca_auto.yaml.example", str(ctx.exception))

    def test_missing_required_paths_raise_with_explicit_path_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "orca_auto.yaml"
            cfg_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("orca.runtime.allowed_root", str(ctx.exception))
            self.assertIn("orca.paths.orca_executable", str(ctx.exception))
            self.assertIn("explicit Linux paths", str(ctx.exception))

    def test_organized_root_collapses_to_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": str(allowed),
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.organized_root, str(allowed))
            self.assertEqual(cfg.runtime.default_max_retries, 2)
            self.assertEqual(cfg.runtime.max_concurrent, 4)

    def test_template_placeholder_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": "/path/to/orca_runs",
                    },
                    "paths": {"orca_executable": "/path/to/orca/orca"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("template placeholder paths", str(ctx.exception))
            self.assertIn("orca.runtime.allowed_root", str(ctx.exception))
            self.assertIn("orca.paths.orca_executable", str(ctx.exception))

    def test_stale_organized_root_key_is_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            organized = root / "orca_outputs"
            organized.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {
                        "allowed_root": str(allowed),
                        "organized_root": str(organized),
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.organized_root, str(allowed))

    def test_nonexistent_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {"allowed_root": str(allowed)},
                    "paths": {"orca_executable": str(root / "nonexistent_orca")},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("orca_executable not found", str(ctx.exception))

    def test_non_executable_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
            fake_orca.chmod(0o644)
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {"allowed_root": str(allowed)},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("orca_executable is not executable", str(ctx.exception))

    def test_nonexistent_allowed_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {"allowed_root": str(root / "nonexistent_dir")},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("allowed_root directory not found", str(ctx.exception))

    def test_allowed_root_is_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            not_a_dir = root / "orca_runs"
            not_a_dir.write_text("oops", encoding="utf-8")
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runtime": {"allowed_root": str(not_a_dir)},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("is not a directory", str(ctx.exception))
