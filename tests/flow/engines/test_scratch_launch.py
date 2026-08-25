from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow.engines import scratch as engine_launch

LOGGER = logging.getLogger(__name__)


def _launch(cfg: Any, tmp_path: Path, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "job_dir": tmp_path,
        "manifest_path": str(tmp_path / "manifest.json"),
        "resource_request": {"max_cores": 1, "max_memory_gb": 1},
        "runtime_environment": {},
        "log_basename": "crest",
        "publish_name": lambda _name: True,
        "clear_stale_outputs": lambda: None,
        "before_popen": None,
        "on_launch_aborted": None,
        "logger": LOGGER,
    }
    kwargs.update(overrides)
    return engine_launch.launch_engine_process(cfg, ["engine"], **kwargs)


def test_launch_aborts_without_a_workspace_when_clearing_stale_outputs_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[Any] = []
    aborted: list[bool] = []
    monkeypatch.setattr(
        engine_launch,
        "publish_engine_scratch_workspace",
        lambda workspace, **_kwargs: published.append(workspace),
    )

    def fail_clear() -> None:
        raise OSError("stale output is not removable")

    with pytest.raises(OSError, match="stale output is not removable"):
        _launch(
            SimpleNamespace(scratch=None),
            tmp_path,
            clear_stale_outputs=fail_clear,
            on_launch_aborted=lambda: aborted.append(True),
        )

    # No workspace exists yet, so there is nothing to publish -- but the caller
    # must still learn the launch was abandoned.
    assert published == []
    assert aborted == [True]


def test_launch_publishes_the_workspace_when_the_process_fails_to_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SimpleNamespace(path=tmp_path / "scratch")
    published: list[Any] = []
    aborted: list[bool] = []
    monkeypatch.setattr(
        engine_launch,
        "create_engine_scratch_workspace",
        lambda *_args, **_kwargs: workspace,
    )
    monkeypatch.setattr(
        engine_launch,
        "scratch_engine_runtime_environment",
        lambda _path, environment: environment,
    )
    monkeypatch.setattr(
        engine_launch,
        "publish_engine_scratch_workspace",
        lambda published_workspace, **_kwargs: published.append(published_workspace),
    )

    def fail_start(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("engine did not start")

    monkeypatch.setattr(engine_launch, "start_logged_process", fail_start)

    with pytest.raises(RuntimeError, match="engine did not start"):
        _launch(
            SimpleNamespace(scratch=None),
            tmp_path,
            on_launch_aborted=lambda: aborted.append(True),
        )

    # The scratch workspace already holds partial output, so it is published back
    # to the durable job directory before the failure propagates.
    assert published == [workspace]
    assert aborted == [True]


def test_launch_runs_before_popen_and_reports_the_scratch_execution_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    workspace = SimpleNamespace(path=scratch_dir)
    calls: list[str] = []
    monkeypatch.setattr(
        engine_launch,
        "create_engine_scratch_workspace",
        lambda *_args, **_kwargs: workspace,
    )
    monkeypatch.setattr(
        engine_launch,
        "scratch_engine_runtime_environment",
        lambda _path, environment: {**environment, "SCRATCH": "1"},
    )

    def fake_start(*_args: Any, **kwargs: Any) -> Any:
        calls.append("start")
        assert kwargs["base_env"] == {"SCRATCH": "1"}
        assert kwargs["cwd"] == scratch_dir
        return SimpleNamespace(process=object(), started_at="2026-04-20T00:00:00Z")

    monkeypatch.setattr(engine_launch, "start_logged_process", fake_start)

    launch = _launch(
        SimpleNamespace(scratch=None),
        tmp_path,
        clear_stale_outputs=lambda: calls.append("clear"),
        before_popen=lambda: calls.append("before_popen"),
    )

    assert calls == ["clear", "before_popen", "start"]
    assert launch.scratch_workspace is workspace
    assert launch.execution_dir == scratch_dir
    assert launch.stdout_log == str((scratch_dir / "crest.stdout.log").resolve())
    assert launch.stderr_log == str((scratch_dir / "crest.stderr.log").resolve())
