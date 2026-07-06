from __future__ import annotations

import stat
from pathlib import Path

import pytest

from orca_auto.core.config.files import (
    engine_config_mapping,
    load_required_yaml_mapping,
    load_yaml_mapping,
    mapping_section,
    resolve_configured_path,
    runs_root_from_mapping,
    scheduler_admission_root,
    secure_config_file_permissions,
)


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


def test_engine_config_mapping_requires_engine_section() -> None:
    raw = {
        "runtime": {"allowed_root": "/tmp/runs"},
        "paths": {"orca_executable": "/tmp/orca"},
        "scheduler": {"max_active_simulations": 4},
    }

    assert engine_config_mapping(raw, "orca", inherit_keys=("scheduler",)) == {}


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
    config_path.write_text("telegram:\n  bot_token: token\n", encoding="utf-8")
    config_path.chmod(0o644)

    secure_config_file_permissions(config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
