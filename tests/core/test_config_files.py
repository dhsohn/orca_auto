from __future__ import annotations

import stat
from pathlib import Path

import pytest

from orca_auto.core.config.files import (
    config_with_canonical_messenger,
    engine_config_mapping,
    load_required_yaml_mapping,
    load_yaml_mapping,
    mapping_section,
    messenger_mapping_from_root,
    resolve_configured_path,
    runs_root_from_mapping,
    scheduler_admission_root,
    secure_config_file_permissions,
    shared_workflow_root_from_config,
    validated_runs_root_text,
)


def test_messenger_mapping_rejects_legacy_top_level_telegram_block() -> None:
    # The migration window is closed: a leftover top-level ``telegram`` block
    # fails closed with a pointed hint instead of being read or silently
    # ignored — even when the canonical nested block is also present.
    legacy = {
        "telegram": {"bot_token": "legacy-token", "chat_id": "legacy-chat"},
    }
    with pytest.raises(ValueError, match="messenger.telegram"):
        messenger_mapping_from_root(legacy)
    with pytest.raises(ValueError, match="no longer supported"):
        messenger_mapping_from_root(
            {
                **legacy,
                "messenger": {
                    "provider": "telegram",
                    "telegram": {"chat_id": "nested-chat"},
                },
            }
        )
    with pytest.raises(ValueError, match="no longer supported"):
        config_with_canonical_messenger(legacy)

    canonical = {
        "messenger": {
            "provider": "telegram",
            "telegram": {"chat_id": "nested-chat"},
        },
    }
    assert messenger_mapping_from_root(canonical) == canonical["messenger"]
    assert config_with_canonical_messenger(canonical)["messenger"] == canonical["messenger"]


def test_messenger_mapping_rejects_non_mapping_new_section() -> None:
    with pytest.raises(ValueError, match="messenger section must be a mapping"):
        messenger_mapping_from_root({"messenger": "telegram"})


def test_yaml_parse_error_does_not_expose_secret_source_line(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    secret = "123456:super-secret-token"
    config_path.write_text(
        f'messenger:\n  telegram:\n    bot_token: "{secret}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as raised:
        load_yaml_mapping(config_path)

    assert secret not in str(raised.value)
    assert str(config_path) in str(raised.value)


def test_runs_root_from_mapping_accepts_only_top_level_key(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"

    # Returns the configured text as-is; callers validate before resolving.
    assert runs_root_from_mapping({"runs_root": str(runs_root)}) == str(runs_root)
    assert runs_root_from_mapping({"runs_root": 0}) == ""
    assert runs_root_from_mapping({}) == ""


def test_runs_root_from_mapping_ignores_removed_legacy_keys(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"

    # The old workflow.root / orca.runtime.allowed_root locations are gone.
    assert (
        runs_root_from_mapping(
            {
                "workflow": {"root": str(runs_root)},
                "orca": {"runtime": {"allowed_root": str(runs_root)}},
            }
        )
        == ""
    )


def test_validated_runs_root_text_rejects_windows_and_relative_values(tmp_path: Path) -> None:
    assert validated_runs_root_text(str(tmp_path / "runs")) == str(tmp_path / "runs")

    with pytest.raises(ValueError, match="Linux path"):
        validated_runs_root_text("C:\\runs")
    with pytest.raises(ValueError, match="Linux path"):
        validated_runs_root_text("/mnt/c/runs")
    with pytest.raises(ValueError, match="absolute Linux path"):
        validated_runs_root_text("./runs")


def test_shared_workflow_root_from_config_returns_none_for_invalid_runs_root(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    config_path = tmp_path / "orca_auto.yaml"

    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    assert shared_workflow_root_from_config(config_path) == str(runs_root.resolve())

    for value in ("'C:\\runs'", "/mnt/c/runs", "./runs"):
        config_path.write_text(f"runs_root: {value}\n", encoding="utf-8")
        assert shared_workflow_root_from_config(config_path) is None


def test_engine_config_mapping_requires_engine_section() -> None:
    raw = {
        "runtime": {"allowed_root": "/tmp/runs"},
        "paths": {"orca_executable": "/tmp/orca"},
        "scheduler": {"max_active_simulations": 4},
    }

    assert engine_config_mapping(raw, "orca", inherit_keys=("scheduler",)) == {}


def test_engine_config_mapping_merges_matching_partial_scheduler_section() -> None:
    raw = {
        "scheduler": {
            "max_active_simulations": 1,
            "admission_root": "/tmp/shared",
        },
        "orca": {
            "scheduler": {"admission_root": "/tmp/shared"},
        },
    }

    assert engine_config_mapping(raw, "orca", inherit_keys=("scheduler",)) == {
        "scheduler": {
            "max_active_simulations": 1,
            "admission_root": "/tmp/shared",
        }
    }


def test_engine_config_mapping_rejects_scheduler_split_brain() -> None:
    raw = {
        "scheduler": {
            "max_active_simulations": 1,
            "admission_root": "/tmp/shared",
        },
        "orca": {
            "scheduler": {"admission_root": "/tmp/orca"},
        },
    }

    with pytest.raises(ValueError, match="cannot override the shared top-level scheduler"):
        engine_config_mapping(raw, "orca", inherit_keys=("scheduler",))


@pytest.mark.parametrize("invalid", [None, "disabled", []])
def test_engine_config_mapping_rejects_non_mapping_engine_scheduler(invalid: object) -> None:
    raw = {
        "scheduler": {"max_active_simulations": 1},
        "orca": {"scheduler": invalid},
    }

    with pytest.raises(ValueError, match="orca.scheduler must be a mapping"):
        engine_config_mapping(raw, "orca", inherit_keys=("scheduler",))


@pytest.mark.parametrize("invalid", [None, "disabled", []])
def test_engine_config_mapping_rejects_non_mapping_engine_resources(invalid: object) -> None:
    raw = {
        "resources": {"max_cores_per_task": 2, "max_memory_gb_per_task": 4},
        "orca": {"resources": invalid},
    }

    with pytest.raises(ValueError, match="orca.resources must be a mapping"):
        engine_config_mapping(raw, "orca", inherit_keys=("resources",))


def test_yaml_mapping_and_section_helpers(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("scheduler:\n  max_active_simulations: 4\n", encoding="utf-8")

    path, raw = load_yaml_mapping(config_path)

    assert path == config_path.resolve()
    assert mapping_section(raw, "scheduler") == {"max_active_simulations": 4}
    assert mapping_section(raw, "missing") == {}

    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("- no\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level is not a mapping"):
        load_yaml_mapping(invalid_path)


def test_required_yaml_mapping_uses_custom_missing_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(ValueError, match="missing config"):
        load_required_yaml_mapping(
            missing,
            missing_error=lambda path: ValueError(f"missing config: {path.name}"),
        )


def test_configured_path_and_admission_root_helpers(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runtime_root = tmp_path / "runtime-admission"
    scheduler_root = tmp_path / "scheduler-admission"

    assert resolve_configured_path("  ") is None
    assert resolve_configured_path(runtime_root) == runtime_root.resolve()
    assert scheduler_admission_root({"admission_root": scheduler_root}) == (
        scheduler_root.resolve()
    )
    assert scheduler_admission_root({}, default_runs_root=runs_root) == (
        runs_root.resolve() / ".admission"
    )
    assert scheduler_admission_root({}) is None


def test_secure_config_file_permissions_sets_owner_only_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("messenger:\n  telegram:\n    bot_token: token\n", encoding="utf-8")
    config_path.chmod(0o644)

    secure_config_file_permissions(config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
