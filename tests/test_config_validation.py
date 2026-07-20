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
    def test_windows_runs_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "C:\\orca_runs",
                    "paths": {"orca_executable": "/opt/orca/orca"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("Linux path", str(ctx.exception))

    def test_windows_mount_runs_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "/mnt/c/orca_runs",
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
                    "runs_root": "./orca_runs",
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
                    "runs_root": "/home/user/orca_runs",
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
                    "runs_root": "/home/user/orca_runs",
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
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.allowed_root, str(allowed))
            self.assertEqual(cfg.paths.orca_executable, str(fake_orca))

    def test_orca_ram_scratch_settings_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = _write_fake_executable(root / "orca")
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                    "runtime": {
                        "scratch_root": "/dev/shm/orca_auto",
                        "scratch_min_free_gb": 8,
                    },
                },
            )

            cfg = load_config(str(cfg_path))

            self.assertTrue(cfg.scratch.enabled)
            self.assertEqual(cfg.scratch.root, "/dev/shm/orca_auto")
            self.assertEqual(cfg.scratch.min_free_gb, 8)

    def test_orca_scratch_root_must_be_below_dev_shm(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = _write_fake_executable(root / "orca")
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                    "runtime": {"scratch_root": str(root / "scratch")},
                },
            )

            with self.assertRaisesRegex(ValueError, "below /dev/shm"):
                load_config(str(cfg_path))

    def test_orca_scratch_minimum_requires_scratch_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = _write_fake_executable(root / "orca")
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                    "runtime": {"scratch_min_free_gb": 8},
                },
            )

            with self.assertRaisesRegex(ValueError, "requires.*scratch_root"):
                load_config(str(cfg_path))

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
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                    "messenger": {
                        "telegram": {
                            "bot_token": "token",
                            "chat_id": "chat",
                            "timeout_seconds": 3.5,
                            "max_attempts": 4,
                            "retry_backoff_seconds": 0.25,
                        },
                    },
                },
            )

            cfg = load_config(str(cfg_path))

            self.assertEqual(cfg.messenger.telegram.bot_token, "token")
            self.assertEqual(cfg.messenger.telegram.chat_id, "chat")
            self.assertEqual(cfg.messenger.telegram.timeout_seconds, 3.5)
            self.assertEqual(cfg.messenger.telegram.max_attempts, 4)
            self.assertEqual(cfg.messenger.telegram.retry_backoff_seconds, 0.25)
            self.assertEqual(cfg.messenger.telegram, cfg.messenger.telegram)

    def test_legacy_top_level_telegram_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                    "telegram": {
                        "bot_token": "legacy-token",
                        "chat_id": "legacy-chat",
                    },
                },
            )

            with self.assertRaisesRegex(ValueError, "messenger.telegram"):
                load_config(str(cfg_path))

    def test_unknown_messenger_provider_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                    "messenger": {"provider": "disocrd"},
                },
            )

            with self.assertRaisesRegex(ValueError, "messenger.provider"):
                load_config(str(cfg_path))

    def test_workflow_root_equals_runs_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "orca": {
                        "paths": {"orca_executable": str(fake_orca)},
                    },
                },
            )

            cfg = load_config(str(cfg_path))

            self.assertEqual(cfg.workflow_root, str(allowed))
            self.assertEqual(cfg.runtime.allowed_root, str(allowed))
            self.assertEqual(cfg.paths.orca_executable, str(fake_orca.resolve()))

    def test_legacy_root_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "workflow": {"root": str(allowed)},
                    "runtime": {"allowed_root": str(allowed)},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )

            with self.assertRaisesRegex(
                ValueError, "Unknown workflow config fields are not supported"
            ):
                load_config(str(cfg_path))

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
                    "runs_root": str(allowed),
                    "runtime": {
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
                    "runs_root": str(allowed),
                    "scheduler": {
                        "max_active_simulations": 6,
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
            self.assertEqual(cfg.runtime.resolved_admission_root, str(allowed / ".admission"))

    def test_removed_orca_runtime_scheduler_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "runtime": {
                        "max_concurrent": 6,
                        "admission_root": str(root / "runtime-admission"),
                        "admission_limit": 3,
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )

            with self.assertRaisesRegex(ValueError, "Unknown orca.runtime config fields"):
                load_config(str(cfg_path))

    def test_engine_scoped_scheduler_is_rejected_even_when_it_matches_shared(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)
            shared_admission = root / "shared-admission"

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "scheduler": {
                        "max_active_simulations": 1,
                        "admission_root": str(shared_admission),
                    },
                    "orca": {
                        "scheduler": {"admission_root": str(shared_admission)},
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )

            with self.assertRaisesRegex(ValueError, "Unknown orca config fields are not supported"):
                load_config(str(cfg_path))

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
                        "runs_root": str(allowed),
                        "scheduler": {"max_active_simulations": value},
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
            self.assertIn("runs_root", str(ctx.exception))
            self.assertIn("orca.paths.orca_executable", str(ctx.exception))
            self.assertIn("explicit Linux paths", str(ctx.exception))

    def test_default_retry_and_concurrency_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.runtime.default_max_retries, 2)
            self.assertEqual(cfg.runtime.max_concurrent, 4)

    def test_template_placeholder_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "/path/to/orca_runs",
                    "paths": {"orca_executable": "/path/to/orca/orca"},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("template placeholder paths", str(ctx.exception))
            self.assertIn("runs_root", str(ctx.exception))
            self.assertIn("orca.paths.orca_executable", str(ctx.exception))

    def test_stale_organized_root_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)

            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
                    "runtime": {
                        "organized_root": str(root / "orca_outputs"),
                    },
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaisesRegex(
                ValueError,
                "Unknown orca.runtime config fields are not supported",
            ):
                load_config(str(cfg_path))

    def test_nonexistent_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(allowed),
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
                    "runs_root": str(allowed),
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("orca_executable is not executable", str(ctx.exception))

    def test_nonexistent_runs_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)
            secret_path = root / "private-runs-secret-missing"
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(secret_path),
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("runs_root directory not found", str(ctx.exception))
            self.assertNotIn(str(secret_path), str(ctx.exception))

    def test_runs_root_is_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            not_a_dir = root / "private-runs-secret-file"
            not_a_dir.write_text("oops", encoding="utf-8")
            fake_orca = root / "orca"
            _write_fake_executable(fake_orca)
            cfg_path = _write_orca_config(
                root / "orca_auto.yaml",
                {
                    "runs_root": str(not_a_dir),
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("is not a directory", str(ctx.exception))
            self.assertNotIn(str(not_a_dir), str(ctx.exception))
