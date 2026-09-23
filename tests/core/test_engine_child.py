from __future__ import annotations

from typing import Any

import pytest

from orca_auto.core.engines import worker_child


def test_worker_child_parser_preserves_spawned_entrypoint_contract() -> None:
    args = worker_child.build_parser().parse_args(
        [
            "--engine",
            "crest",
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

    assert args.engine == "crest"
    assert args.config == "/tmp/orca_auto.yaml"
    assert args.queue_root == "/tmp/queue"
    assert args.queue_id == "queue-1"
    assert args.admission_token == "slot-1"


def test_worker_child_parser_requires_the_engine_it_dispatches_on() -> None:
    with pytest.raises(SystemExit):
        worker_child.build_parser().parse_args(
            [
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

    def fake_run_engine_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 37

    monkeypatch.setattr(
        worker_child,
        "run_engine_worker_child_job",
        fake_run_engine_worker_child_job,
    )

    result = worker_child.main(
        [
            "--engine",
            "xtb",
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
    assert captured["engine"] == "xtb"
    assert captured["config_path"] == "/tmp/orca_auto.yaml"
    assert captured["queue_root"] == "/tmp/queue"
    assert captured["queue_id"] == "q-1"
    assert captured["admission_token"] == "slot-1"


def test_worker_child_main_treats_a_blank_admission_token_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        worker_child,
        "run_engine_worker_child_job",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    worker_child.main(
        [
            "--engine",
            "orca",
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
