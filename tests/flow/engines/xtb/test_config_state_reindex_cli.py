from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orca_auto.core.artifacts import XTB_JOB_MANIFEST_FILE
from orca_auto.core.config.engines import (
    CONFIG_ENV_VAR,
    as_bool,
    as_int,
    as_str,
)
from orca_auto.core.config.engines import (
    default_shared_config_path as default_config_path,
)
from orca_auto.core.config.engines import (
    load_xtb_config as load_config,
)
from orca_auto.flow.engines.xtb import state as state_mod
from tests.engine_artifact_helpers import artifact_payload


def test_default_config_path_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "/tmp/custom-orca_auto.yaml")
    assert default_config_path() == "/tmp/custom-orca_auto.yaml"

    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert default_config_path().endswith("/config/orca_auto.yaml")


def _write_fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_load_config_parses_defaults_and_normalizes_values(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    fake_xtb = _write_fake_executable(tmp_path / "bin" / "xtb")
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runs_root": str(workflow_root),
                "scheduler": {
                    "max_active_simulations": "6",
                },
                "workflow": {
                    "paths": {
                        "xtb_executable": f" {fake_xtb} ",
                    },
                },
                "behavior": {
                    "auto_organize_on_terminal": "yes",
                },
                "resources": {
                    "max_cores_per_task": "1",
                    "max_memory_gb_per_task": "1",
                },
                "messenger": {
                    "telegram": {
                        "bot_token": " token ",
                        "chat_id": " chat ",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(str(config_path))

    assert cfg.runtime.allowed_root == str(workflow_root.resolve())
    assert cfg.runtime.max_concurrent == 6
    assert cfg.runtime.admission_root == str(workflow_root.resolve() / ".admission")
    assert cfg.runtime.admission_limit == 6
    assert cfg.paths.xtb_executable == str(fake_xtb.resolve())
    assert cfg.resources.max_cores_per_task == 1
    assert cfg.resources.max_memory_gb_per_task == 1
    assert cfg.messenger.telegram.bot_token == "token"
    assert cfg.messenger.telegram.chat_id == "chat"


def test_load_config_reports_missing_file_invalid_payload_and_requires_workflow_root(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.yaml"
    with pytest.raises(ValueError, match="Config file not found"):
        load_config(str(missing_path))

    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Config file is invalid"):
        load_config(str(invalid_path))

    missing_workflow_root_path = tmp_path / "missing-workflow-root.yaml"
    missing_workflow_root_path.write_text(
        yaml.safe_dump({"xtb": {"runtime": {}}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"Config is missing runs_root"):
        load_config(str(missing_workflow_root_path))


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, False, False),
        (None, True, True),
        (True, False, True),
        (False, True, False),
        ("YES", False, True),
        ("off", True, False),
        ("maybe", True, True),
        ("maybe", False, False),
    ],
)
def test_helper_normalizers_cover_boolean_and_default_branches(
    value: object,
    default: bool,
    expected: bool,
) -> None:
    assert as_bool(value, default) == expected


def test_helper_normalizers_cover_string_and_int_defaults() -> None:
    assert as_str(None, "fallback") == "fallback"
    assert as_str("  value  ", "fallback") == "value"
    assert as_int(None, 7) == 7
    assert as_int("9", 7) == 9
    assert as_int("not-a-number", 7) == 7


def test_load_config_applies_defaults_for_missing_and_legacy_optional_sections(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runs_root": str(workflow_root),
                "behavior": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(str(config_path))

    assert cfg.runtime.allowed_root == str(workflow_root.resolve())
    assert cfg.runtime.max_concurrent == 4
    assert cfg.runtime.admission_root == str(workflow_root.resolve() / ".admission")
    assert cfg.runtime.admission_limit == 4
    assert cfg.paths.xtb_executable == ""
    assert cfg.resources.max_cores_per_task == 8
    assert cfg.resources.max_memory_gb_per_task == 32
    assert cfg.messenger.telegram.bot_token == ""
    assert cfg.messenger.telegram.chat_id == ""


def test_state_helper_writes_only_canonical_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job-001"
    job_dir.mkdir()
    monkeypatch.setattr(state_mod, "now_utc_iso", lambda: "2026-04-20T00:00:00Z")

    state_path = state_mod.write_state(job_dir, {"status": "queued"})

    assert state_path == job_dir / state_mod.STATE_FILE_NAME
    assert state_mod.load_state(job_dir) == {"status": "queued"}
    assert not (job_dir / "job_report.json").exists()
    assert not (job_dir / "job_report.md").exists()


def test_state_loader_returns_none_for_missing_invalid_and_non_mapping_payloads(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job-002"
    job_dir.mkdir()

    assert state_mod.load_state(job_dir) is None
    path = job_dir / state_mod.STATE_FILE_NAME
    path.write_text("{invalid-json", encoding="utf-8")
    assert state_mod.load_state(job_dir) is None
    path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert state_mod.load_state(job_dir) is None


def test_mark_recovery_pending_preserves_xtb_schema_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job-003"
    job_dir.mkdir()
    manifest = job_dir / XTB_JOB_MANIFEST_FILE
    manifest.write_text("job_id: old-job\n", encoding="utf-8")
    monkeypatch.setattr(state_mod, "now_utc_iso", lambda: "2026-04-20T01:02:03Z")

    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id="old-job",
            job_dir=str(job_dir),
            status="running",
            resource_request={"cores": 8},
            resource_actual={"cores": 4},
            created_at="2026-04-19T00:00:00Z",
            started_at="2026-04-19T00:01:00Z",
            recovery_count=3,
            artifacts={"manifest_path": str(manifest.resolve())},
            engine_payload={
                "candidate_count": 2,
                "candidate_paths": ["/tmp/old-a.xyz"],
                "selected_candidate_paths": ["/tmp/old-best.xyz"],
                "candidate_details": [{"path": "/tmp/old-a.xyz"}],
                "analysis_summary": {"best": "/tmp/old-best.xyz"},
            },
        ),
    )

    payload = state_mod.mark_recovery_pending(
        job_dir,
        job_id="new-job",
        selected_input_xyz=job_dir / "selected.xyz",
        job_type=" path_search ",
        reaction_key=" rxn-1 ",
        input_summary={"candidate_paths": ["/tmp/from-summary.xyz"]},
        resource_request=None,
        resource_actual={"cores": 1},
        reason=" worker_shutdown ",
    )

    assert state_mod.load_state(job_dir) == payload
    assert payload["job"]["id"] == "old-job"
    assert payload["engine_payload"]["job_type"] == "path_search"
    assert payload["engine_payload"]["reaction_key"] == "rxn-1"
    assert payload["engine_payload"]["input_summary"] == {
        "candidate_paths": ["/tmp/from-summary.xyz"]
    }
    assert payload["engine_payload"]["candidate_count"] == 2
    assert payload["engine_payload"]["candidate_paths"] == ["/tmp/old-a.xyz"]
    assert payload["engine_payload"]["selected_candidate_paths"] == ["/tmp/old-best.xyz"]
    assert payload["engine_payload"]["candidate_details"] == [{"path": "/tmp/old-a.xyz"}]
    assert payload["engine_payload"]["analysis_summary"] == {"best": "/tmp/old-best.xyz"}
    assert payload["artifacts"]["manifest_path"] == str(manifest.resolve())
    assert payload["resources"]["request"] == {"cores": 8}
    assert payload["resources"]["actual"] == {"cores": 1}
    assert payload["recovery"]["count"] == 4
    assert payload["recovery"]["pending"] is True
