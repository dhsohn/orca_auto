from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest

from orca_auto.cli import main
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.worker.process import PidFileChildProcessQueueWorker
from orca_auto.orca.queue import adapter
from tests.core.test_queue_worker_common import _cfg, _worker_deps
from tests.test_orca_worker_execution import _queued_submission


def test_cancel_does_not_require_available_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, config_path, entry, executable = _queued_submission(tmp_path)
    executable.unlink()
    assert main(["queue", "cancel", entry.queue_id, "--config", str(config_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cancelled"
    current = adapter.get_entry_by_id(Path(cfg.runtime.allowed_root), entry.queue_id)
    assert current is not None and current.status.value == "cancelled"


@pytest.mark.parametrize("json_output", [False, True])
def test_cancel_failure_returns_error_without_success_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], json_output: bool
) -> None:
    cfg, config_path, entry, _executable = _queued_submission(tmp_path)
    assert adapter.mark_failed(Path(cfg.runtime.allowed_root), entry.queue_id, error="test failure")
    args = ["queue", "cancel", entry.queue_id, "--config", str(config_path)]
    if json_output:
        args.append("--json")
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already terminal" in captured.err


@pytest.mark.parametrize("method", ["run", "run_once"])
def test_worker_body_timeout_is_not_duplicate_worker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], method: str
) -> None:
    class Worker(PidFileChildProcessQueueWorker):
        def _install_signal_handlers(self) -> None:
            pass

        def _before_run(self) -> None:
            pass

        def _after_run(self) -> None:
            pass

        def _shutdown_all(self) -> None:
            pass

        def _reserve_next_entry(self) -> NoReturn:
            raise QueueLockTimeoutError("actual queue lock timed out")

    deps = _worker_deps(poll_interval_seconds=0)
    cfg = _cfg(allowed_root=str(tmp_path), max_concurrent=1)
    worker = Worker(cfg, config_path="unused", deps=deps)
    with pytest.raises(QueueLockTimeoutError, match="actual queue lock"):
        getattr(worker, method)()
    assert "already running" not in capsys.readouterr().err
