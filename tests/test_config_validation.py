import json
import tempfile
from pathlib import Path

import pytest
import yaml

from orca_auto.core.config.schema import messenger_config_from_mapping
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


class TestConfigValidation:
    def test_windows_runs_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "C:\\orca_runs",
                    "paths": {"orca_executable": "/opt/orca/orca"},
                },
            )
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "Linux path" in str(exc_info.value)

    def test_windows_mount_runs_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "/mnt/c/orca_runs",
                    "paths": {"orca_executable": "/home/user/opt/orca/orca"},
                },
            )
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "Linux path" in str(exc_info.value)

    def test_relative_paths_raise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "./orca_runs",
                    "paths": {"orca_executable": "./opt/orca/orca"},
                },
            )
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "absolute Linux path" in str(exc_info.value)

    def test_windows_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "/home/user/orca_runs",
                    "paths": {"orca_executable": "C:\\Orca\\orca.exe"},
                },
            )
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "Linux path" in str(exc_info.value)

    def test_exe_suffix_orca_executable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "/home/user/orca_runs",
                    "paths": {"orca_executable": "/home/user/opt/orca/orca.exe"},
                },
            )
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "Linux ORCA binary" in str(exc_info.value)

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
            assert cfg.runtime.allowed_root == str(allowed)
            assert cfg.paths.orca_executable == str(fake_orca)

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

            assert cfg.scratch.enabled
            assert cfg.scratch.root == "/dev/shm/orca_auto"
            assert cfg.scratch.min_free_gb == 8

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

            with pytest.raises(ValueError, match="below /dev/shm"):
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

            with pytest.raises(ValueError, match="requires.*scratch_root"):
                load_config(str(cfg_path))

    def test_discord_delivery_settings_are_loaded(self) -> None:
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
                        "discord": {
                            "bot_token": "token",
                            "default_channel_id": "123",
                            "timeout_seconds": 3.5,
                            "max_attempts": 4,
                            "retry_backoff_seconds": 0.25,
                        },
                    },
                },
            )

            cfg = load_config(str(cfg_path))

            assert cfg.messenger.discord.bot_token == "token"
            assert cfg.messenger.discord.default_channel_id == "123"
            assert cfg.messenger.discord.timeout_seconds == 3.5
            assert cfg.messenger.discord.max_attempts == 4
            assert cfg.messenger.discord.retry_backoff_seconds == 0.25

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

            with pytest.raises(ValueError, match="messenger.provider"):
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

            assert cfg.workflow_root == str(allowed)
            assert cfg.runtime.allowed_root == str(allowed)
            assert cfg.paths.orca_executable == str(fake_orca.resolve())

    def test_runtime_has_no_retry_setting(self) -> None:
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
                    "runtime": {},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )
            cfg = load_config(str(cfg_path))
            assert not hasattr(cfg.runtime, "default_max_retries")
            assert cfg.runtime.max_concurrent == 4

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

            assert cfg.resources.max_cores_per_task == 12
            assert cfg.resources.max_memory_gb_per_task == 48

            assert cfg.runtime.allowed_root == str(allowed)
            assert cfg.runtime.max_concurrent == 6
            assert cfg.runtime.resolved_admission_limit == 6
            assert cfg.runtime.resolved_admission_root == str(allowed / ".admission")

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

            with pytest.raises(ValueError, match="Unknown orca.runtime config fields"):
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

            with pytest.raises(ValueError, match="Unknown orca config fields are not supported"):
                load_config(str(cfg_path))

    @pytest.mark.parametrize("value", ["bad", 0, -1, True])
    def test_scheduler_max_active_simulations_rejects_invalid_explicit_values(
        self,
        value: object,
    ) -> None:
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
                    "scheduler": {"max_active_simulations": value},
                    "paths": {"orca_executable": str(fake_orca)},
                },
            )

            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "scheduler.max_active_simulations must be an integer >= 1" in str(exc_info.value)

    def test_missing_config_file_raises_with_setup_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "orca_auto.yaml"
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "Config file not found" in str(exc_info.value)
            assert "orca_auto.yaml.example" in str(exc_info.value)

    def test_shipped_example_config_messenger_matches_current_schema(self) -> None:
        """bootstrap copies this template verbatim, so it must stay loadable."""
        example = Path(__file__).resolve().parents[1] / "config" / "orca_auto.yaml.example"
        raw = yaml.safe_load(example.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        messenger = messenger_config_from_mapping(raw.get("messenger"))
        assert messenger.normalized_provider == "discord"

    def test_missing_required_paths_raise_with_explicit_path_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "orca_auto.yaml"
            cfg_path.write_text("{}", encoding="utf-8")
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "runs_root" in str(exc_info.value)
            assert "orca.paths.orca_executable" in str(exc_info.value)
            assert "explicit Linux paths" in str(exc_info.value)

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
            assert not hasattr(cfg.runtime, "default_max_retries")
            assert cfg.runtime.max_concurrent == 4

    def test_template_placeholder_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = _write_orca_config(
                Path(td) / "orca_auto.yaml",
                {
                    "runs_root": "/path/to/orca_runs",
                    "paths": {"orca_executable": "/path/to/orca/orca"},
                },
            )
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "template placeholder paths" in str(exc_info.value)
            assert "runs_root" in str(exc_info.value)
            assert "orca.paths.orca_executable" in str(exc_info.value)

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
            with pytest.raises(
                ValueError,
                match="Unknown orca.runtime config fields are not supported",
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
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "orca_executable not found" in str(exc_info.value)

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
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "orca_executable is not executable" in str(exc_info.value)

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
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "runs_root directory not found" in str(exc_info.value)
            assert str(secret_path) not in str(exc_info.value)

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
            with pytest.raises(ValueError) as exc_info:
                load_config(str(cfg_path))
            assert "is not a directory" in str(exc_info.value)
            assert str(not_a_dir) not in str(exc_info.value)
