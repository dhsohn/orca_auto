from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.config.engines import load_crest_config, load_xtb_config

Loader = Callable[[str | None], Any]


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
