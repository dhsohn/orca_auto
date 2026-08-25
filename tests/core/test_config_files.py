from __future__ import annotations

import stat
from pathlib import Path

import pytest

from orca_auto.core.config.files import (
    engine_config_mapping,
    load_shared_config_mapping,
    load_yaml_mapping,
    mapping_section,
    messenger_mapping_from_root,
    resolve_configured_path,
    runs_root_from_mapping,
    scheduler_admission_root,
    secure_config_file_permissions,
    shared_workflow_root_from_config,
    validate_shared_config_sections,
    validated_runs_root_text,
)
from orca_auto.core.paths.validation import validated_absolute_linux_path_text


def test_messenger_mapping_reads_messenger_section() -> None:
    canonical = {
        "messenger": {
            "provider": "discord",
            "discord": {"default_channel_id": "123"},
        },
    }
    assert messenger_mapping_from_root(canonical) == canonical["messenger"]


@pytest.mark.parametrize("invalid", [None, "telegram", []])
def test_messenger_mapping_rejects_non_mapping_new_section(invalid: object) -> None:
    with pytest.raises(ValueError, match="messenger section must be a mapping"):
        messenger_mapping_from_root({"messenger": invalid})


def test_yaml_parse_error_does_not_expose_secret_source_line(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    secret = "123456:super-secret-token"
    config_path.write_text(
        f'messenger:\n  discord:\n    bot_token: "{secret}\n',
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


def test_validated_runs_root_text_rejects_windows_and_relative_values(tmp_path: Path) -> None:
    assert validated_runs_root_text(str(tmp_path / "runs")) == str(tmp_path / "runs")

    with pytest.raises(ValueError, match="Linux path"):
        validated_runs_root_text("C:\\runs")
    with pytest.raises(ValueError, match="Linux path"):
        validated_runs_root_text("/mnt/c/runs")
    with pytest.raises(ValueError, match="absolute Linux path"):
        validated_runs_root_text("./runs")


@pytest.mark.parametrize(
    "field_name",
    ["runs_root", "scheduler.admission_root", "orca.runtime.scratch_root"],
)
@pytest.mark.parametrize(
    "secret_path",
    [
        "private-path-secret",
        r"C:\private-path-secret",
        "/tmp/../mnt/c/private-path-secret",
    ],
)
def test_canonical_config_path_errors_do_not_echo_raw_values(
    field_name: str,
    secret_path: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        validated_absolute_linux_path_text(secret_path, field_name=field_name)

    message = str(captured.value)
    assert field_name in message
    assert "private-path-secret" not in message


def test_runs_root_validation_error_does_not_echo_raw_value() -> None:
    with pytest.raises(ValueError) as captured:
        validated_runs_root_text("private-runs-root-secret")

    assert "runs_root" in str(captured.value)
    assert "private-runs-root-secret" not in str(captured.value)


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


def test_engine_config_mapping_rejects_redundant_engine_scoped_scheduler() -> None:
    raw = {
        "scheduler": {
            "max_active_simulations": 1,
            "admission_root": "/tmp/shared",
        },
        "orca": {
            "scheduler": {"admission_root": "/tmp/shared"},
        },
    }

    with pytest.raises(ValueError, match="orca.scheduler is not supported"):
        engine_config_mapping(raw, "orca", inherit_keys=("scheduler",))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"schedulr": {}}, "Unknown top-level config fields are not supported"),
        (
            {"scheduler": {"max_active_simulation": 4}},
            "Unknown scheduler config fields are not supported",
        ),
        (
            {"resources": {"max_core_per_task": 8}},
            "Unknown resources config fields are not supported",
        ),
        (
            {"workflow": {"root": "/tmp/runs"}},
            "Unknown workflow config fields are not supported",
        ),
        (
            {"workflow": {"paths": {"xtb_path": "/tmp/xtb"}}},
            "Unknown workflow.paths config fields are not supported",
        ),
        (
            {"orca": {"runtime": {"max_concurrent": 2}}},
            "Unknown orca.runtime config fields are not supported",
        ),
        (
            {"orca": {"paths": {"executable": "/tmp/orca"}}},
            "Unknown orca.paths config fields are not supported",
        ),
    ],
)
def test_shared_config_validation_rejects_unknown_fields(
    raw: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_shared_config_sections(raw)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            "resources:\n  max_cores_per_task: invalid\n",
            "resources.max_cores_per_task must be an integer >= 1",
        ),
        (
            "resources:\n  max_memory_gb_per_task: 0\n",
            "resources.max_memory_gb_per_task must be an integer >= 1",
        ),
        (
            "scheduler:\n  admission_root: relative/pool\n",
            "scheduler.admission_root must be an absolute Linux path",
        ),
        (
            "orca:\n  runtime:\n    scratch_min_free_gb: 8\n",
            "orca.runtime.scratch_min_free_gb requires orca.runtime.scratch_root",
        ),
        (
            "orca:\n  runtime:\n    scratch_root: /tmp/orca-scratch\n",
            "orca.runtime.scratch_root must be a dedicated directory below /dev/shm",
        ),
        (
            "orca:\n  runtime:\n    scratch_root: /dev/shm/orca-scratch\n"
            "    scratch_min_free_gb: 0\n",
            "orca.runtime.scratch_min_free_gb must be an integer >= 1",
        ),
    ],
)
def test_complete_shared_loader_rejects_malformed_execution_controls(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_shared_config_mapping(config_path)


@pytest.mark.parametrize(
    "payload",
    [
        "misplaced-credential: true\n",
        "messenger:\n  provider: misplaced-credential\n",
        "messenger:\n  discord:\n    uploads:\n      max_archive_bytes: misplaced-credential\n",
        "scheduler:\n  admission_root: misplaced-credential\n",
        "orca:\n  runtime:\n    scratch_root: misplaced-credential\n",
    ],
)
def test_shared_config_errors_do_not_echo_misplaced_credentials(
    tmp_path: Path,
    payload: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError) as captured:
        load_shared_config_mapping(config_path)

    assert "misplaced-credential" not in str(captured.value)


def test_engine_config_mapping_rejects_engine_scoped_scheduler_split_brain() -> None:
    raw = {
        "scheduler": {
            "max_active_simulations": 1,
            "admission_root": "/tmp/shared",
        },
        "orca": {
            "scheduler": {"admission_root": "/tmp/orca"},
        },
    }

    with pytest.raises(ValueError, match="orca.scheduler is not supported"):
        engine_config_mapping(raw, "orca", inherit_keys=("scheduler",))


@pytest.mark.parametrize("invalid", [None, "disabled", []])
def test_engine_config_mapping_rejects_non_mapping_engine_scheduler(invalid: object) -> None:
    raw = {
        "scheduler": {"max_active_simulations": 1},
        "orca": {"scheduler": invalid},
    }

    with pytest.raises(ValueError, match="orca.scheduler is not supported"):
        engine_config_mapping(raw, "orca", inherit_keys=("scheduler",))


@pytest.mark.parametrize("invalid", [None, "disabled", []])
def test_engine_config_mapping_rejects_non_mapping_engine_resources(invalid: object) -> None:
    raw = {
        "resources": {"max_cores_per_task": 2, "max_memory_gb_per_task": 4},
        "orca": {"resources": invalid},
    }

    with pytest.raises(ValueError, match="orca.resources is not supported"):
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


@pytest.mark.parametrize("payload", ["", "  \n\t", "# comment only\n"])
def test_yaml_mapping_treats_documents_without_a_yaml_node_as_empty_mapping(
    tmp_path: Path,
    payload: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    _path, raw = load_yaml_mapping(config_path)

    assert raw == {}


@pytest.mark.parametrize(
    "payload",
    [
        "null\n",
        "~\n",
        "---\n",
        "---\n# comment\n",
        '!!null ""\n',
        '--- !!null ""\n',
        "false\n",
        "0\n",
        "[]\n",
        "text\n",
    ],
)
def test_yaml_mapping_rejects_explicit_non_mapping_documents(
    tmp_path: Path,
    payload: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="top-level is not a mapping"):
        load_yaml_mapping(config_path)


@pytest.mark.parametrize(
    "payload",
    [
        "runs_root: /tmp/one\nruns_root: /tmp/two\n",
        "scheduler:\n  max_active_simulations: 1\n  max_active_simulations: 2\n",
    ],
)
def test_yaml_mapping_rejects_duplicate_keys_at_every_depth(
    tmp_path: Path,
    payload: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate mapping key"):
        load_yaml_mapping(config_path)


def test_duplicate_key_error_does_not_expose_secret_values(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    first_secret = "first-super-secret-token"
    second_secret = "second-super-secret-token"
    config_path.write_text(
        f"messenger:\n  discord:\n    bot_token: {first_secret}\n    bot_token: {second_secret}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as raised:
        load_yaml_mapping(config_path)

    message = str(raised.value)
    assert "duplicate mapping key" in message
    assert first_secret not in message
    assert second_secret not in message


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
    config_path.write_text("messenger:\n  discord:\n    bot_token: token\n", encoding="utf-8")
    config_path.chmod(0o644)

    secure_config_file_permissions(config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
