from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.config.engines import load_crest_config, load_xtb_config, load_xtb_md_config
from orca_auto.orca.config import load_config as load_orca_config

Loader = Callable[[str], Any]

_SHARED_CONFIG_LOADERS: tuple[tuple[str, Loader], ...] = (
    ("orca", load_orca_config),
    ("xtb", load_xtb_config),
    ("crest", load_crest_config),
)


def _write_config(
    tmp_path: Path,
    *,
    path_key: str,
    executable_value: str,
) -> Path:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir(exist_ok=True)
    config_path = tmp_path / f"{path_key}.yaml"
    config_path.write_text(
        json.dumps(
            {
                "runs_root": str(workflow_root),
                "workflow": {
                    "paths": {path_key: executable_value},
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


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
        "workflow": {"paths": {}},
        "orca": {"paths": {"orca_executable": str(orca_executable)}},
    }
    payload.update(override)
    config_path = tmp_path / "shared-config.yaml"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


@pytest.mark.parametrize(("loader_name", "loader"), _SHARED_CONFIG_LOADERS)
@pytest.mark.parametrize("invalid", [None, "disabled", []])
@pytest.mark.parametrize(
    ("section_path", "message"),
    [
        pytest.param("scheduler", "scheduler section must be a mapping", id="scheduler"),
        pytest.param("resources", "resources section must be a mapping", id="resources"),
        pytest.param("workflow", "workflow section must be a mapping", id="workflow"),
        pytest.param(
            "workflow.paths",
            "workflow.paths section must be a mapping",
            id="workflow-paths",
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
    if section_path == "workflow.paths":
        override = {"workflow": {"paths": invalid}}
    else:
        override = {section_path: invalid}
    config_path = _write_shared_config(tmp_path, override)

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


@pytest.mark.parametrize(
    ("loader", "path_key"),
    [
        pytest.param(load_xtb_config, "xtb_executable", id="xtb"),
        pytest.param(load_crest_config, "crest_executable", id="crest"),
    ],
)
def test_workflow_engine_config_accepts_existing_linux_executable(
    tmp_path: Path,
    loader: Loader,
    path_key: str,
) -> None:
    executable = _write_file(
        tmp_path / "bin" / path_key.replace("_executable", ""), executable=True
    )
    config_path = _write_config(
        tmp_path,
        path_key=path_key,
        executable_value=f" {executable} ",
    )

    cfg = loader(str(config_path))

    assert getattr(cfg.paths, path_key) == str(executable.resolve())


@pytest.mark.parametrize(
    ("loader", "path_key"),
    [
        pytest.param(load_xtb_config, "xtb_executable", id="xtb"),
        pytest.param(load_crest_config, "crest_executable", id="crest"),
    ],
)
def test_workflow_engine_config_keeps_blank_executable_for_path_fallback(
    tmp_path: Path,
    loader: Loader,
    path_key: str,
) -> None:
    config_path = _write_config(tmp_path, path_key=path_key, executable_value="  ")

    cfg = loader(str(config_path))

    assert getattr(cfg.paths, path_key) == ""


def test_xtb_md_config_uses_standalone_root_and_default_engine_cap(tmp_path: Path) -> None:
    executable = _write_file(tmp_path / "bin" / "xtb", executable=True)
    config_path = _write_shared_config(
        tmp_path,
        {
            "scheduler": {"max_active_simulations": 5},
            "workflow": {"paths": {"xtb_executable": str(executable)}},
        },
    )

    cfg = load_xtb_md_config(str(config_path))

    assert cfg.runtime.allowed_root == str((tmp_path / "runs").resolve())
    assert cfg.runtime.max_concurrent == 5
    assert cfg.runtime.admission_limit == 5
    assert cfg.runtime.engine_admission_limit == 1
    assert cfg.workflow_root == ""
    assert cfg.paths.xtb_executable == str(executable.resolve())


def test_xtb_md_config_accepts_explicit_engine_cap(tmp_path: Path) -> None:
    config_path = _write_shared_config(
        tmp_path,
        {
            "scheduler": {
                "max_active_simulations": 7,
                "max_active_xtb_md": 3,
            }
        },
    )

    cfg = load_xtb_md_config(str(config_path))

    assert cfg.runtime.max_concurrent == 7
    assert cfg.runtime.admission_limit == 7
    assert cfg.runtime.engine_admission_limit == 3


@pytest.mark.parametrize("invalid", [None, "", 0, -1, True, False, 1.5])
def test_xtb_md_config_rejects_invalid_explicit_engine_cap(
    tmp_path: Path,
    invalid: object,
) -> None:
    config_path = _write_shared_config(
        tmp_path,
        {"scheduler": {"max_active_xtb_md": invalid}},
    )

    with pytest.raises(
        ValueError,
        match="scheduler.max_active_xtb_md must be an integer >= 1",
    ):
        load_xtb_md_config(str(config_path))


@pytest.mark.parametrize(
    ("raw_path", "message"),
    [
        pytest.param("relative/tool", "absolute Linux path", id="relative"),
        pytest.param(r"C:\\tools\\xtb.exe", "Linux path", id="windows-backslash"),
        pytest.param("C:/tools/xtb.exe", "Linux path", id="windows-forward-slash"),
        pytest.param("C:tools/xtb.exe", "Linux path", id="windows-drive-relative"),
        pytest.param("/mnt/c/tools/xtb.exe", "Linux path", id="wsl-windows-mount"),
    ],
)
@pytest.mark.parametrize(
    ("loader", "path_key"),
    [
        pytest.param(load_xtb_config, "xtb_executable", id="xtb"),
        pytest.param(load_crest_config, "crest_executable", id="crest"),
    ],
)
def test_workflow_engine_config_rejects_non_linux_executable_path_syntax(
    tmp_path: Path,
    loader: Loader,
    path_key: str,
    raw_path: str,
    message: str,
) -> None:
    config_path = _write_config(tmp_path, path_key=path_key, executable_value=raw_path)

    with pytest.raises(ValueError, match=message):
        loader(str(config_path))


@pytest.mark.parametrize(
    ("loader", "path_key", "display_name"),
    [
        pytest.param(load_xtb_config, "xtb_executable", "xTB", id="xtb"),
        pytest.param(load_crest_config, "crest_executable", "CREST", id="crest"),
    ],
)
def test_workflow_engine_config_rejects_windows_exe_suffix(
    tmp_path: Path,
    loader: Loader,
    path_key: str,
    display_name: str,
) -> None:
    executable = _write_file(tmp_path / "bin" / f"{display_name.lower()}.exe", executable=True)
    config_path = _write_config(tmp_path, path_key=path_key, executable_value=str(executable))

    with pytest.raises(ValueError, match=rf"Linux {display_name} binary"):
        loader(str(config_path))


@pytest.mark.parametrize(
    ("loader", "path_key"),
    [
        pytest.param(load_xtb_config, "xtb_executable", id="xtb"),
        pytest.param(load_crest_config, "crest_executable", id="crest"),
    ],
)
def test_workflow_engine_config_rejects_missing_directory_and_non_executable_files(
    tmp_path: Path,
    loader: Loader,
    path_key: str,
) -> None:
    missing_path = tmp_path / "bin" / "missing-tool"
    missing_config = _write_config(tmp_path, path_key=path_key, executable_value=str(missing_path))
    with pytest.raises(ValueError, match=rf"{path_key} not found"):
        loader(str(missing_config))

    directory = tmp_path / "bin" / "tool-dir"
    directory.mkdir(parents=True)
    directory_config = _write_config(tmp_path, path_key=path_key, executable_value=str(directory))
    with pytest.raises(ValueError, match=rf"{path_key} is not a file"):
        loader(str(directory_config))

    not_executable = _write_file(tmp_path / "bin" / "tool-not-executable", executable=False)
    not_executable_config = _write_config(
        tmp_path,
        path_key=path_key,
        executable_value=str(not_executable),
    )
    with pytest.raises(ValueError, match=rf"{path_key} is not executable"):
        loader(str(not_executable_config))
