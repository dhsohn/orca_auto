from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca import worker_execution
from orca_auto.orca.cli_logging import remove_managed_handlers
from orca_auto.orca.commands import worker_child


def test_worker_child_parser_preserves_spawned_entrypoint_contract() -> None:
    args = worker_child.build_parser().parse_args(
        [
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "queue-1",
            "--admission-token",
            "slot-1",
        ]
    )

    assert args.config == "/tmp/orca_auto.yaml"
    assert args.queue_root == "/tmp/queue"
    assert args.queue_id == "queue-1"
    assert args.admission_token == "slot-1"


@pytest.mark.parametrize("missing", ["--config", "--queue-root", "--queue-id"])
def test_worker_child_parser_requires_the_queue_identity(missing: str) -> None:
    argv = [
        "--config",
        "/tmp/orca_auto.yaml",
        "--queue-root",
        "/tmp/queue",
        "--queue-id",
        "queue-1",
    ]
    index = argv.index(missing)
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        worker_child.build_parser().parse_args(argv)


def test_worker_child_parser_rejects_the_retired_engine_selector() -> None:
    with pytest.raises(SystemExit):
        worker_child.build_parser().parse_args(
            [
                "--engine",
                "orca",
                "--config",
                "/tmp/orca_auto.yaml",
                "--queue-root",
                "/tmp/queue",
                "--queue-id",
                "queue-1",
            ]
        )


def test_worker_child_main_dispatches_the_parsed_queue_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 37

    monkeypatch.setattr(worker_child, "run_worker_child_job", fake_run_worker_child_job)

    result = worker_child.main(
        [
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "q-1",
            "--admission-token",
            " slot-1 ",
        ]
    )

    assert result == 37
    assert captured == {
        "config_path": "/tmp/orca_auto.yaml",
        "queue_root": "/tmp/queue",
        "queue_id": "q-1",
        "admission_token": "slot-1",
    }


def test_worker_child_main_treats_a_blank_admission_token_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        worker_child,
        "run_worker_child_job",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    worker_child.main(
        [
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "q-1",
            "--admission-token",
            "   ",
        ]
    )

    assert captured["admission_token"] is None


def test_worker_child_main_configures_logging_so_info_reaches_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root_logger = logging.getLogger()
    previous_level = root_logger.level

    def fake_run_worker_child_job(**kwargs: Any) -> int:
        logging.getLogger("orca_auto.orca.worker_execution").info("child job started")
        return 0

    monkeypatch.setattr(worker_child, "run_worker_child_job", fake_run_worker_child_job)
    try:
        rc = worker_child.main(
            ["--config", "/tmp/orca_auto.yaml", "--queue-root", "/tmp/queue", "--queue-id", "q-1"]
        )
    finally:
        remove_managed_handlers(root_logger)
        root_logger.setLevel(previous_level)

    assert rc == 0
    assert "[INFO] orca_auto.orca.worker_execution: child job started" in capsys.readouterr().err


def test_spawned_child_command_round_trips_through_the_child_parser(tmp_path: Path) -> None:
    command = worker_execution.build_worker_child_command(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="q-1",
        admission_token="slot-1",
    )

    assert command[:3] == [sys.executable, "-m", "orca_auto.orca.commands.worker_child"]
    assert "--engine" not in command
    args = worker_child.build_parser().parse_args(command[3:])
    assert (args.config, args.queue_root, args.queue_id, args.admission_token) == (
        "/tmp/orca_auto.yaml",
        str(tmp_path / "queue"),
        "q-1",
        "slot-1",
    )

    without_token = worker_execution.build_worker_child_command(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="q-1",
    )
    assert "--admission-token" not in without_token
