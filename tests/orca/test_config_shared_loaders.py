from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.config import load_config as load_orca_config
from orca_auto.orca.config import (
    load_orca_shared_config_mapping,
    validate_orca_shared_config,
)

Loader = Callable[[str], Any]

_SHARED_CONFIG_LOADERS: tuple[tuple[str, Loader], ...] = (("orca", load_orca_config),)


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
def test_shared_engine_loaders_use_orca_runtime_scratch_policy(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
) -> None:
    del loader_name
    config_path = _write_shared_config(
        tmp_path,
        {
            "orca": {
                "paths": {
                    "orca_executable": str(_write_file(tmp_path / "bin" / "orca", executable=True))
                },
                "runtime": {
                    "scratch_root": "/dev/shm/orca_auto",
                    "scratch_min_free_gb": 7,
                },
            }
        },
    )

    cfg = loader(str(config_path))

    assert cfg.scratch.root == "/dev/shm/orca_auto"
    assert cfg.scratch.min_free_gb == 7


def _write_file(path: Path, *, executable: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def _write_shared_config(tmp_path: Path, override: dict[str, object]) -> Path:
    runs_root = tmp_path / "runs"
    runs_root.mkdir(exist_ok=True)
    orca_executable = _write_file(tmp_path / "bin" / "orca", executable=True)
    payload: dict[str, object] = {
        "runs_root": str(runs_root),
        "orca": {"paths": {"orca_executable": str(orca_executable)}},
    }
    payload.update(override)
    config_path = tmp_path / "shared-config.yaml"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


@pytest.mark.parametrize(
    ("loader", "override", "field_name"),
    [
        pytest.param(
            load_orca_config,
            {"orca": {"paths": {"orca_executable": "misplaced-executable-secret"}}},
            "orca_executable",
            id="orca",
        ),
    ],
)
def test_configured_executable_errors_do_not_echo_raw_values(
    tmp_path: Path,
    loader: Loader,
    override: dict[str, object],
    field_name: str,
) -> None:
    config_path = _write_shared_config(tmp_path, override)

    with pytest.raises(ValueError) as captured:
        loader(str(config_path))

    message = str(captured.value)
    assert field_name in message
    assert "absolute Linux path" in message
    assert "misplaced-executable-secret" not in message


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize("invalid", [None, "disabled", []])
@pytest.mark.parametrize(
    ("section_path", "message"),
    [
        pytest.param("scheduler", "scheduler section must be a mapping", id="scheduler"),
        pytest.param("resources", "resources section must be a mapping", id="resources"),
        pytest.param("orca", "orca section must be a mapping", id="orca"),
        pytest.param(
            "orca.runtime",
            "orca.runtime section must be a mapping",
            id="orca-runtime",
        ),
        pytest.param(
            "orca.paths",
            "orca.paths section must be a mapping",
            id="orca-paths",
        ),
        pytest.param("messenger", "messenger section must be a mapping", id="messenger"),
        pytest.param(
            "messenger.discord",
            "messenger.discord must be a mapping",
            id="messenger-discord",
        ),
    ],
)
def test_shared_engine_loaders_reject_non_mapping_execution_sections(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    invalid: object,
    section_path: str,
    message: str,
) -> None:
    del loader_name
    override: dict[str, object]
    if section_path == "orca.runtime":
        override = {"orca": {"runtime": invalid}}
    elif section_path == "orca.paths":
        override = {"orca": {"paths": invalid}}
    elif section_path == "messenger.discord":
        override = {"messenger": {"discord": invalid}}
    else:
        override = {section_path: invalid}
    config_path = _write_shared_config(tmp_path, override)

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize(
    ("override", "message"),
    [
        pytest.param(
            {"runs_root": None},
            "runs_root must be a string when configured",
            id="runs-root-null",
        ),
        pytest.param(
            {"orca": {"paths": {"orca_executable": None}}},
            "orca.paths.orca_executable must be a string when configured",
            id="orca-executable-null",
        ),
    ],
)
def test_shared_engine_loaders_reject_null_explicit_paths(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    override: dict[str, object],
    message: str,
) -> None:
    del loader_name
    config_path = _write_shared_config(tmp_path, override)

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize(
    ("override", "message"),
    [
        pytest.param(
            {"schedulr": {}},
            "Unknown top-level config fields are not supported",
            id="top-level-typo",
        ),
        pytest.param(
            {"scheduler": {"max_active_simulation": 2}},
            "Unknown scheduler config fields are not supported",
            id="scheduler-typo",
        ),
        pytest.param(
            {"resources": {"max_core_per_task": 2}},
            "Unknown resources config fields are not supported",
            id="resources-typo",
        ),
        pytest.param(
            {"orca": {"scheduler": {"max_active_simulations": 2}}},
            "Unknown orca config fields are not supported",
            id="engine-scoped-scheduler",
        ),
        pytest.param(
            {"orca": {"runtime": {"max_concurrent": 2}}},
            "Unknown orca.runtime config fields are not supported",
            id="removed-runtime-key",
        ),
        pytest.param(
            {"messenger": {"provder": "discord"}},
            "Unknown messenger config fields are not supported",
            id="messenger-typo",
        ),
        pytest.param(
            {"messenger": {"discord": {"channe_ids": []}}},
            "Unknown messenger.discord config fields are not supported",
            id="discord-typo",
        ),
    ],
)
def test_shared_engine_loaders_reject_unknown_config_fields(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    override: dict[str, object],
    message: str,
) -> None:
    del loader_name
    config_path = _write_shared_config(tmp_path, override)

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize("invalid", [None, "", "bad", -1, True, 1.5, 0, 1, 3])
def test_shared_engine_loaders_reject_removed_orca_retry_setting(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    invalid: object,
) -> None:
    del loader_name
    config_path = _write_shared_config(
        tmp_path,
        {"orca": {"runtime": {"default_max_retries": invalid}}},
    )

    with pytest.raises(
        ValueError,
        match="Unknown orca.runtime config fields",
    ):
        loader(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize("invalid", [None, "", 1, True])
def test_shared_engine_loaders_reject_invalid_explicit_scratch_root(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    invalid: object,
) -> None:
    del loader_name
    config_path = _write_shared_config(
        tmp_path,
        {"orca": {"runtime": {"scratch_root": invalid}}},
    )

    with pytest.raises(
        ValueError,
        match="orca.runtime.scratch_root must be a non-empty string",
    ):
        loader(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize(
    ("messenger", "message"),
    [
        pytest.param(
            {"discord": {"max_attempts": True}},
            "messenger.discord.max_attempts must be an integer",
            id="discord-delivery-bool",
        ),
        pytest.param(
            {"discord": {"bot_token": True}},
            "messenger.discord.bot_token must be a string",
            id="discord-token-bool",
        ),
        pytest.param(
            {"discord": {"default_channel_id": None}},
            "messenger.discord.default_channel_id",
            id="discord-default-channel-null",
        ),
        pytest.param(
            {"discord": {"channel_ids": []}},
            "Unknown messenger.discord config fields are not supported",
            id="discord-interactive-key-removed",
        ),
    ],
)
def test_shared_engine_loaders_reject_invalid_explicit_messenger_values(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    messenger: dict[str, object],
    message: str,
) -> None:
    del loader_name
    config_path = _write_shared_config(tmp_path, {"messenger": messenger})

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize(
    ("raw_path", "message"),
    [
        pytest.param("relative/pool", "absolute Linux path", id="relative"),
        pytest.param("", "absolute Linux path", id="blank"),
        pytest.param(None, "absolute Linux path", id="null"),
        pytest.param(r"C:\\runtime\\pool", "Linux path", id="windows"),
        pytest.param("/mnt/c/runtime/pool", "Linux path", id="wsl-windows-mount"),
        pytest.param(
            "/tmp/../mnt/c/runtime/pool",
            "outside Windows mounts",
            id="normalized-wsl-windows-mount",
        ),
    ],
)
def test_shared_engine_loaders_reject_invalid_admission_root(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    raw_path: object,
    message: str,
) -> None:
    del loader_name
    config_path = _write_shared_config(
        tmp_path,
        {"scheduler": {"admission_root": raw_path}},
    )

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


def test_orca_loader_rejects_runs_root_that_canonicalizes_to_windows_mount(
    tmp_path: Path,
) -> None:
    config_path = _write_shared_config(
        tmp_path,
        {
            "runs_root": "/tmp/../mnt/c/orca-runs",
            "scheduler": {"admission_root": str(tmp_path / "admission")},
        },
    )

    with pytest.raises(ValueError, match="outside Windows mounts"):
        load_orca_config(str(config_path))


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize("invalid", [None, "", "bad", 0, -1, True, 1.5])
@pytest.mark.parametrize(
    ("section", "key", "message"),
    [
        pytest.param(
            "scheduler",
            "max_active_simulations",
            "scheduler.max_active_simulations must be an integer >= 1",
            id="max-active-simulations",
        ),
        pytest.param(
            "resources",
            "max_cores_per_task",
            "resources.max_cores_per_task must be an integer >= 1",
            id="max-cores-per-task",
        ),
        pytest.param(
            "resources",
            "max_memory_gb_per_task",
            "resources.max_memory_gb_per_task must be an integer >= 1",
            id="max-memory-per-task",
        ),
    ],
)
def test_shared_engine_loaders_reject_invalid_execution_limits(
    tmp_path: Path,
    loader_name: str,
    loader: Loader,
    invalid: object,
    section: str,
    key: str,
    message: str,
) -> None:
    del loader_name
    config_path = _write_shared_config(tmp_path, {section: {key: invalid}})

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


def test_orca_sections_return_every_configured_model() -> None:
    shared, orca_sections = validate_orca_shared_config(
        {
            "runs_root": "/tmp/runs",
            "orca": {
                "runtime": {"scratch_root": "/dev/shm/orca-scratch", "scratch_min_free_gb": 2},
                "paths": {"orca_executable": "/opt/orca/orca"},
            },
        }
    )

    assert shared.runs_root == "/tmp/runs"
    assert orca_sections.orca_executable == "/opt/orca/orca"
    assert orca_sections.scratch.root == "/dev/shm/orca-scratch"
    assert orca_sections.scratch.min_free_gb == 2


def test_orca_sections_apply_defaults_once() -> None:
    _shared, orca_sections = validate_orca_shared_config({})

    assert orca_sections.orca_executable == ""
    assert not orca_sections.scratch.enabled


@pytest.mark.parametrize("section", ["scheduler", "resources", "messenger"])
@pytest.mark.parametrize("invalid", [None, "disabled", [], {"admission_root": "/tmp/shared"}])
def test_engine_scoped_shared_sections_are_rejected(section: str, invalid: object) -> None:
    # resources, messenger and scheduler are top-level only; an orca.* copy is
    # rejected before any inheritance question can arise.
    raw = {
        "scheduler": {"max_active_simulations": 1, "admission_root": "/tmp/shared"},
        "orca": {section: invalid},
    }

    with pytest.raises(ValueError, match="Unknown orca config fields are not supported"):
        validate_orca_shared_config(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
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
def test_orca_section_validation_rejects_unknown_fields(
    raw: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_orca_shared_config(raw)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
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
def test_complete_orca_loader_rejects_malformed_scratch_controls(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_orca_shared_config_mapping(config_path)


def test_orca_section_errors_do_not_echo_misplaced_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "orca:\n  runtime:\n    scratch_root: misplaced-credential\n", encoding="utf-8"
    )

    with pytest.raises(ValueError) as captured:
        load_orca_shared_config_mapping(config_path)

    assert "misplaced-credential" not in str(captured.value)
