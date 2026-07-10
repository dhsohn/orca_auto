from __future__ import annotations

import json
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import CREST_JOB_MANIFEST_FILE
from orca_auto.core.config import engines as config_mod
from orca_auto.flow.engines.crest import state as state_mod
from tests.engine_artifact_helpers import (
    artifact_payload,
)
from tests.engine_artifact_helpers import (
    artifacts as _artifacts,
)
from tests.engine_artifact_helpers import (
    engine_payload as _engine_payload,
)
from tests.engine_artifact_helpers import (
    job as _job,
)
from tests.engine_artifact_helpers import (
    recovery as _recovery,
)
from tests.engine_artifact_helpers import (
    resources as _resources,
)

JsonWriter = Callable[[Path, dict[str, Any]], Path]
JsonLoader = Callable[[Path], dict[str, Any] | None]


def _write_config(path: Path, contents: str) -> Path:
    path.write_text(textwrap.dedent(contents).strip() + "\n", encoding="utf-8")
    return path


def test_default_config_path_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config_mod.CONFIG_ENV_VAR, "  ~/custom-config.yaml  ")

    assert config_mod.default_shared_config_path() == "~/custom-config.yaml"


@pytest.mark.parametrize("env_value", [None, "   "], ids=["unset", "blank"])
def test_default_config_path_falls_back_to_repo_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_value: str | None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    if env_value is None:
        monkeypatch.delenv(config_mod.CONFIG_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(config_mod.CONFIG_ENV_VAR, env_value)

    expected = str(Path(config_mod.__file__).resolve().parents[4] / "config" / "orca_auto.yaml")

    assert config_mod.default_shared_config_path() == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, "fallback", "fallback"),
        ("  crest  ", "", "crest"),
        (123, "", "123"),
    ],
)
def test_as_str_normalizes_values(value: object, default: str, expected: str) -> None:
    assert config_mod.as_str(value, default) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("7", 3, 7),
        (5.9, 0, 5),
        ("not-an-int", 9, 9),
        (None, 4, 4),
    ],
)
def test_as_int_returns_default_for_invalid_values(
    value: object, default: int, expected: int
) -> None:
    assert config_mod.as_int(value, default) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, True, True),
        (True, False, True),
        (" yes ", False, True),
        ("OFF", True, False),
        ("maybe", True, True),
    ],
)
def test_as_bool_normalizes_truthy_and_falsy_strings(
    value: object,
    default: bool,
    expected: bool,
) -> None:
    assert config_mod.as_bool(value, default) is expected


def _write_fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_load_config_reads_and_normalizes_all_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    fake_crest = _write_fake_executable(tmp_path / "bin" / "crest")
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        f"""
        runs_root: {workflow_root}
        scheduler:
          max_active_simulations: "6"
          admission_root: /tmp/admission
        workflow:
          paths:
            crest_executable: " {fake_crest} "
        behavior:
          auto_organize_on_terminal: "yes"
        resources:
          max_cores_per_task: "12"
          max_memory_gb_per_task: "48"
        messenger:
          telegram:
            bot_token: " token-123 "
            chat_id: " 4567 "
        """,
    )
    monkeypatch.setattr(config_mod, "default_shared_config_path", lambda: str(config_path))

    cfg = config_mod.load_crest_config()

    assert cfg.runtime.allowed_root == str(workflow_root.resolve())
    assert cfg.runtime.max_concurrent == 6
    assert cfg.runtime.admission_root == "/tmp/admission"
    assert cfg.runtime.admission_limit == 6
    assert cfg.paths.crest_executable == str(fake_crest.resolve())
    assert not hasattr(cfg.behavior, "auto_organize_on_terminal")
    assert cfg.resources.max_cores_per_task == 12
    assert cfg.resources.max_memory_gb_per_task == 48
    assert cfg.telegram.bot_token == "token-123"
    assert cfg.telegram.chat_id == "4567"


def test_load_config_no_longer_supports_top_level_runtime_and_paths_shape(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        """
        scheduler:
          max_active_simulations: "6"
          admission_root: /tmp/admission
        runtime:
          allowed_root: /tmp/runs
        paths:
          crest_executable: " /opt/crest "
        behavior:
          auto_organize_on_terminal: "yes"
        resources:
          max_cores_per_task: "12"
          max_memory_gb_per_task: "48"
        messenger:
          telegram:
            bot_token: " token-123 "
            chat_id: " 4567 "
        """,
    )

    with pytest.raises(ValueError, match=r"Config is missing runs_root"):
        config_mod.load_crest_config(str(config_path))


def test_load_config_applies_defaults_for_missing_or_invalid_sections(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        f"""
        runs_root: {workflow_root}
        scheduler:
          max_active_simulations: 1
        workflow:
          paths: []
        behavior: invalid
        resources: nope
        telegram: []
        """,
    )

    cfg = config_mod.load_crest_config(str(config_path))

    assert cfg.runtime.allowed_root == str(workflow_root.resolve())
    assert cfg.runtime.max_concurrent == 1
    assert cfg.runtime.admission_root == str(workflow_root.resolve() / ".admission")
    assert cfg.runtime.admission_limit == 1
    assert cfg.paths.crest_executable == ""
    assert not hasattr(cfg.behavior, "auto_organize_on_terminal")
    assert cfg.resources.max_cores_per_task == 8
    assert cfg.resources.max_memory_gb_per_task == 32
    assert cfg.telegram.bot_token == ""
    assert cfg.telegram.chat_id == ""


@pytest.mark.parametrize("value", [0, -1, "bad", True])
def test_load_config_rejects_invalid_explicit_scheduler_max_active_simulations(
    tmp_path: Path,
    value: object,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        f"""
        runs_root: {workflow_root}
        scheduler:
          max_active_simulations: {value!r}
        """,
    )

    with pytest.raises(
        ValueError, match="scheduler.max_active_simulations must be an integer >= 1"
    ):
        config_mod.load_crest_config(str(config_path))


def test_load_config_rejects_missing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.yaml"

    with pytest.raises(ValueError, match="Config file not found"):
        config_mod.load_crest_config(str(missing_path))


def test_load_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        """
        - not
        - a
        - mapping
        """,
    )

    with pytest.raises(ValueError, match="Config file is invalid"):
        config_mod.load_crest_config(str(config_path))


def test_load_config_requires_workflow_root(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        """
        workflow:
          paths:
            crest_executable: /opt/crest
        """,
    )

    with pytest.raises(ValueError, match=r"Config is missing runs_root"):
        config_mod.load_crest_config(str(config_path))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param("C:\\runs", "Linux path", id="windows-drive"),
        pytest.param("/mnt/c/runs", "Linux path", id="windows-mount"),
        pytest.param("./runs", "absolute Linux path", id="relative"),
    ],
)
def test_load_config_rejects_invalid_runs_root_before_resolving(
    tmp_path: Path,
    value: str,
    message: str,
) -> None:
    config_path = _write_config(
        tmp_path / "orca_auto.yaml",
        f"""
        runs_root: '{value}'
        """,
    )

    with pytest.raises(ValueError, match=message):
        config_mod.load_crest_config(str(config_path))
    with pytest.raises(ValueError, match=message):
        config_mod.load_xtb_config(str(config_path))


@pytest.mark.parametrize(
    ("writer", "loader", "filename"),
    [
        pytest.param(
            state_mod.write_state,
            state_mod.load_state,
            state_mod.STATE_FILE_NAME,
            id="state",
        ),
        pytest.param(
            state_mod.write_report_json,
            state_mod.load_report_json,
            state_mod.REPORT_JSON_FILE_NAME,
            id="report-json",
        ),
    ],
)
def test_json_state_helpers_round_trip(
    tmp_path: Path,
    writer: JsonWriter,
    loader: JsonLoader,
    filename: str,
) -> None:
    job_dir = tmp_path / "job"
    payload = {"status": "running", "attempt": 1}

    path = writer(job_dir, payload)

    assert path == job_dir / filename
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert loader(job_dir) == payload


@pytest.mark.parametrize(
    ("loader", "filename"),
    [
        pytest.param(state_mod.load_state, state_mod.STATE_FILE_NAME, id="state"),
        pytest.param(state_mod.load_report_json, state_mod.REPORT_JSON_FILE_NAME, id="report-json"),
    ],
)
def test_json_state_helpers_return_none_for_missing_invalid_and_non_object_payloads(
    tmp_path: Path,
    loader: JsonLoader,
    filename: str,
) -> None:
    job_dir = tmp_path / "job"

    assert loader(job_dir) is None

    job_dir.mkdir()
    (job_dir / filename).write_text("{invalid json", encoding="utf-8")
    assert loader(job_dir) is None

    (job_dir / filename).write_text('["not", "an", "object"]', encoding="utf-8")
    assert loader(job_dir) is None


def test_write_report_md_writes_expected_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    monkeypatch.setattr(state_mod, "now_utc_iso", lambda: "2026-04-19T00:00:00+00:00")

    path = state_mod.write_report_md(
        job_dir,
        job_id="crest-123",
        status="completed",
        reason="ok",
        selected_xyz="/tmp/input.xyz",
    )

    assert path == job_dir / state_mod.REPORT_MD_FILE_NAME
    assert path.read_text(encoding="utf-8") == (
        "# orca_auto CREST Report\n"
        "\n"
        "- Job ID: `crest-123`\n"
        "- Status: `completed`\n"
        "- Reason: `ok`\n"
        "- Selected XYZ: `/tmp/input.xyz`\n"
        "- Updated At: `2026-04-19T00:00:00+00:00`\n"
    )


def test_write_report_md_lines_writes_lines_with_trailing_newline(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    path = state_mod.write_report_md_lines(job_dir, ["# Custom Report", "", "- Item: `value`"])

    assert path == job_dir / state_mod.REPORT_MD_FILE_NAME
    assert path.read_text(encoding="utf-8") == "# Custom Report\n\n- Item: `value`\n"


def test_mark_recovery_pending_preserves_crest_schema_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job-recovery"
    job_dir.mkdir()
    manifest = job_dir / CREST_JOB_MANIFEST_FILE
    manifest.write_text("job_id: crest-old\n", encoding="utf-8")
    monkeypatch.setattr(state_mod, "now_utc_iso", lambda: "2026-04-20T01:02:03Z")

    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="crest",
            job_id="crest-old",
            job_dir=str(job_dir.resolve()),
            created_at="2026-04-19T00:00:00Z",
            started_at="2026-04-19T00:01:00Z",
            resource_request={"cores": 16},
            resource_actual={"cores": 8},
            recovery_count=1,
            engine_payload={
                "retained_conformer_count": 2,
                "retained_conformer_paths": ["/tmp/conf-a.xyz", "/tmp/conf-b.xyz"],
            },
        ),
    )

    payload = state_mod.mark_recovery_pending(
        job_dir,
        job_id="crest-new",
        selected_input_xyz=job_dir / "selected.xyz",
        mode=" standard ",
        molecule_key=" mol-1 ",
        resource_request={"cores": 4},
        resource_actual=None,
        reason=" crashed_recovery ",
    )

    assert state_mod.load_state(job_dir) == payload
    assert _job(payload)["id"] == "crest-old"
    assert _engine_payload(payload)["mode"] == "standard"
    assert _engine_payload(payload)["molecule_key"] == "mol-1"
    assert _engine_payload(payload)["retained_conformer_count"] == 2
    assert _engine_payload(payload)["retained_conformer_paths"] == [
        "/tmp/conf-a.xyz",
        "/tmp/conf-b.xyz",
    ]
    assert "candidate_paths" not in _engine_payload(payload)
    assert "input_summary" not in _engine_payload(payload)
    assert _artifacts(payload)["manifest_path"] == str(manifest.resolve())
    assert _resources(payload)["request"] == {"cores": 4}
    assert _resources(payload)["actual"] == {"cores": 8}
    assert _recovery(payload)["count"] == 2
    assert _recovery(payload)["pending"] is True
