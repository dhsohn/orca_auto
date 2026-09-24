"""Tests for orca_auto.orca.commands.queue foreground worker behavior."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.queue.worker.pid_file import write_worker_pid_file
from orca_auto.orca.cli_logging import remove_managed_handlers
from orca_auto.orca.commands import queue as queue_command
from orca_auto.orca.commands.queue import cmd_queue_worker
from orca_auto.orca.config import load_config


class _FakeWorker:
    """Records the constructor call and answers ``run`` with a fixed exit code."""

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    exit_code = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).calls.append((args, kwargs))

    def run(self) -> int:
        return type(self).exit_code


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> type[_FakeWorker]:
    _FakeWorker.calls = []
    _FakeWorker.exit_code = 0
    monkeypatch.setattr(queue_command, "OrcaQueueWorker", _FakeWorker)
    return _FakeWorker


def test_worker_already_running(
    tmp_path: Path,
    config_path: Callable[..., Path],
    fake_worker: type[_FakeWorker],
) -> None:
    config = config_path()
    # A live worker pid file under the configured runs root refuses a second worker.
    write_worker_pid_file(tmp_path)

    rc = cmd_queue_worker(argparse.Namespace(config=str(config)))

    assert rc == 1
    assert fake_worker.calls == []


def test_worker_runs_in_foreground_only(
    config_path: Callable[..., Path],
    fake_worker: type[_FakeWorker],
) -> None:
    config = config_path()
    fake_worker.exit_code = 7

    rc = cmd_queue_worker(argparse.Namespace(config=str(config)))

    assert rc == 7
    assert fake_worker.calls == [
        ((load_config(str(config)), str(config)), {"max_concurrent": 4}),
    ]


def test_worker_uses_config_max_concurrent_when_flag_omitted(
    config_path: Callable[..., Path],
    fake_worker: type[_FakeWorker],
) -> None:
    config = config_path(max_concurrent=6)

    rc = cmd_queue_worker(argparse.Namespace(config=str(config)))

    assert rc == 0
    assert fake_worker.calls == [
        ((load_config(str(config)), str(config)), {"max_concurrent": 6}),
    ]


def test_main_configures_logging_so_worker_info_reaches_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root_logger = logging.getLogger()
    previous_level = root_logger.level

    def fake_cmd_queue_worker(args: argparse.Namespace) -> int:
        logging.getLogger("orca_auto.orca.queue.worker").info("Queue worker started")
        return 0

    monkeypatch.setattr(queue_command, "cmd_queue_worker", fake_cmd_queue_worker)
    try:
        assert queue_command.main(["--config", "/tmp/orca_auto.yaml"]) == 0
    finally:
        remove_managed_handlers(root_logger)
        root_logger.setLevel(previous_level)

    assert "[INFO] orca_auto.orca.queue.worker: Queue worker started" in capsys.readouterr().err
