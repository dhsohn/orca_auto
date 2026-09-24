from __future__ import annotations

import stat
from pathlib import Path

import pytest

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config.files import (
    SharedConfig,
    default_config_path,
    discover_shared_config_path,
    load_shared_config,
    load_shared_config_mapping,
    load_yaml_mapping,
    mapping_section,
    messenger_mapping_from_root,
    resolve_configured_path,
    resolved_admission_root,
    secure_config_file_permissions,
    usable_runs_root_text,
    validate_shared_config_sections,
    validated_runs_root_text,
)
from orca_auto.core.config.schema import SchedulerConfig
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


def test_validated_sections_carry_runs_root_text_verbatim(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"

    # Text is returned as configured; strict consumers validate before resolving.
    assert validate_shared_config_sections({"runs_root": str(runs_root)}).runs_root == str(
        runs_root
    )
    assert validate_shared_config_sections({}).runs_root == ""
    with pytest.raises(ValueError, match="runs_root must be a string"):
        validate_shared_config_sections({"runs_root": 0})


def test_usable_runs_root_text_ignores_invalid_values(tmp_path: Path) -> None:
    assert usable_runs_root_text(str(tmp_path / "runs")) == str(tmp_path / "runs")
    assert usable_runs_root_text("") == ""
    assert usable_runs_root_text("./runs") == ""
    assert usable_runs_root_text("C:\\runs") == ""


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


def test_validated_sections_apply_schema_defaults_once() -> None:
    shared = validate_shared_config_sections({})

    assert shared == SharedConfig()
    assert shared.scheduler == SchedulerConfig(max_active_simulations=4, configured=False)
    assert shared.scheduler.admission_limit is None
    assert (shared.resources.max_cores_per_task, shared.resources.max_memory_gb_per_task) == (
        8,
        32,
    )
    assert shared.engine_section == {}
    assert not shared.messenger.enabled


def test_validated_sections_return_every_configured_model(tmp_path: Path) -> None:
    shared = validate_shared_config_sections(
        {
            "runs_root": "/tmp/runs",
            "scheduler": {"max_active_simulations": "6", "admission_root": "/tmp/pool"},
            "resources": {"max_cores_per_task": 12},
            "orca": {
                "runtime": {"scratch_root": "/dev/shm/orca-scratch", "scratch_min_free_gb": 2},
                "paths": {"orca_executable": "/opt/orca/orca"},
            },
            "messenger": {"discord": {"bot_token": "token", "default_channel_id": "123"}},
        }
    )

    assert shared.runs_root == "/tmp/runs"
    assert shared.scheduler == SchedulerConfig(
        max_active_simulations=6, admission_root="/tmp/pool", configured=True
    )
    assert shared.scheduler.admission_limit == 6
    assert shared.resources.max_cores_per_task == 12
    assert shared.resources.max_memory_gb_per_task == 32
    # The engine section is passed through raw for ``orca_auto.orca.config``.
    assert shared.engine_section == {
        "runtime": {"scratch_root": "/dev/shm/orca-scratch", "scratch_min_free_gb": 2},
        "paths": {"orca_executable": "/opt/orca/orca"},
    }
    assert shared.messenger.enabled


def test_scheduler_section_with_only_admission_root_pins_default_limit() -> None:
    shared = validate_shared_config_sections({"scheduler": {"admission_root": "/tmp/pool"}})

    assert shared.scheduler.configured
    assert shared.scheduler.admission_limit == 4


@pytest.mark.parametrize("invalid", [None, "disabled", []])
def test_engine_section_must_be_a_mapping(invalid: object) -> None:
    with pytest.raises(ValueError, match="orca section must be a mapping"):
        validate_shared_config_sections({"orca": invalid})


def test_load_shared_config_requires_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(FileNotFoundError):
        load_shared_config(missing)
    with pytest.raises(ValueError, match="custom missing"):
        load_shared_config(missing, missing_error=lambda path: ValueError(f"custom missing {path}"))

    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("runs_root: /tmp/runs\n", encoding="utf-8")
    path, shared = load_shared_config(config_path)
    assert path == config_path.resolve()
    assert shared.runs_root == "/tmp/runs"


def test_discovery_order_is_explicit_then_env_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(ORCA_AUTO_CONFIG_ENV_VAR, raising=False)
    home_default = home / "orca_auto" / "config" / "orca_auto.yaml"

    assert default_config_path() == str(home_default)
    assert discover_shared_config_path(None) is None

    home_default.parent.mkdir(parents=True)
    home_default.write_text("{}\n", encoding="utf-8")
    assert discover_shared_config_path(None) == str(home_default.resolve())

    env_config = tmp_path / "env.yaml"
    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(env_config))
    assert default_config_path() == str(env_config)
    # An environment path is reported even before the file exists.
    assert discover_shared_config_path(None) == str(env_config.resolve())

    explicit = tmp_path / "explicit.yaml"
    assert discover_shared_config_path(str(explicit)) == str(explicit.resolve())


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
            "Unknown top-level config fields are not supported",
        ),
        (
            {"workflow": {"paths": {"xtb_path": "/tmp/xtb"}}},
            "Unknown top-level config fields are not supported",
        ),
    ],
)
def test_shared_config_validation_rejects_unknown_fields(
    raw: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_shared_config_sections(raw)


def test_shared_config_unknown_field_error_does_not_echo_raw_key() -> None:
    secret_key = "private-secret-key"

    with pytest.raises(ValueError) as raised:
        validate_shared_config_sections({secret_key: {}})

    assert secret_key not in str(raised.value)


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


def test_yaml_mapping_rejects_unhashable_mapping_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("? [alpha, beta]\n: value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping keys must be hashable scalars"):
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
    explicit = SchedulerConfig(admission_root=str(scheduler_root), configured=True)
    assert resolved_admission_root(explicit) == scheduler_root.resolve()
    assert resolved_admission_root(explicit, runs_root=runs_root) == scheduler_root.resolve()
    assert resolved_admission_root(SchedulerConfig(), runs_root=runs_root) == (
        runs_root.resolve() / ".admission"
    )
    assert resolved_admission_root(SchedulerConfig()) is None


def test_secure_config_file_permissions_sets_owner_only_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("messenger:\n  discord:\n    bot_token: token\n", encoding="utf-8")
    config_path.chmod(0o644)

    secure_config_file_permissions(config_path)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
