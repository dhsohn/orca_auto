from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.commands import queue as shared_queue_cmd
from orca_auto.flow.engines.crest import queue_runtime as queue_cmd
from tests.engine_process_helpers import process_one_crest_for_test


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            SimpleNamespace(
                cancel_requested=True,
                status=SimpleNamespace(value="running"),
            ),
            "cancel_requested",
        ),
        (
            SimpleNamespace(
                cancel_requested=False,
                status=SimpleNamespace(value=" "),
            ),
            "unknown",
        ),
    ],
)
def test_queue_display_status(entry: object, expected: str) -> None:
    assert shared_queue_cmd.display_status(entry) == expected


def test_queue_worker_parser_has_no_organize_flags() -> None:
    args = queue_cmd.build_parser().parse_args(["--config", "/tmp/orca_auto.yaml"])

    assert args.config == "/tmp/orca_auto.yaml"
    assert not hasattr(args, "auto_organize")
    assert not hasattr(args, "no_auto_organize")

    with pytest.raises(SystemExit):
        queue_cmd.build_parser().parse_args(
            ["--config", "/tmp/orca_auto.yaml", "--no-auto-organize"]
        )


def test_cmd_queue_worker_constructs_crest_worker_without_organize_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root="/tmp/allowed",
            max_concurrent=2,
        ),
    )
    seen: list[tuple[object, str, int]] = []

    monkeypatch.setattr(queue_cmd, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(queue_cmd, "read_worker_pid", lambda allowed_root: None)

    class FakeWorker:
        def __init__(
            self,
            cfg_obj: object,
            config_path: str,
            *,
            max_concurrent: int,
        ) -> None:
            seen.append((cfg_obj, config_path, max_concurrent))

        def run(self) -> int:
            return 0

    monkeypatch.setattr(queue_cmd, "QueueWorker", FakeWorker)

    result = queue_cmd.cmd_queue_worker(
        SimpleNamespace(
            config="ignored",
        )
    )

    assert result == 0
    assert seen == [(cfg, "ignored", 2)]
    assert capsys.readouterr().out == ""


def test_process_one_returns_blocked_when_no_admission_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root="ignored"))

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda cfg_obj: None)

    assert process_one_crest_for_test(queue_cmd, cfg) == "blocked"


def test_process_one_returns_idle_and_releases_reserved_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path / "allowed"),
            admission_root="",
            resolved_admission_root=None,
        )
    )
    released: list[tuple[str, str | None]] = []

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda cfg_obj: "slot-1")
    monkeypatch.setattr(queue_cmd, "dequeue_next", lambda root, **_kw: None)
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((root, token))
    )

    assert process_one_crest_for_test(queue_cmd, cfg) == "idle"
    assert released == [(cfg.runtime.allowed_root, "slot-1")]


def test_cmd_queue_worker_runs_pool_worker_when_not_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root="/tmp/allowed",
            max_concurrent=3,
        )
    )
    constructed: list[tuple[object, str, int]] = []
    run_calls: list[bool] = []

    monkeypatch.setattr(queue_cmd, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(queue_cmd, "read_worker_pid", lambda allowed_root: None)

    class FakeWorker:
        def __init__(self, cfg_obj: object, config_path: str, *, max_concurrent: int) -> None:
            constructed.append((cfg_obj, config_path, max_concurrent))

        def run(self) -> int:
            run_calls.append(True)
            return 17

    monkeypatch.setattr(queue_cmd, "QueueWorker", FakeWorker)

    result = queue_cmd.cmd_queue_worker(
        SimpleNamespace(
            config="ignored",
        )
    )

    assert result == 17
    assert constructed == [(cfg, "ignored", 3)]
    assert run_calls == [True]
