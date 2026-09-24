from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest

from orca_auto.cli import main
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.orca.queue import adapter
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import make_app_cfg
from tests.queue_worker_helpers import queued_submission


def test_cancel_does_not_require_available_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, config_path, entry, executable = queued_submission(tmp_path)
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
    cfg, config_path, entry, _executable = queued_submission(tmp_path)
    assert adapter.mark_failed(Path(cfg.runtime.allowed_root), entry.queue_id, error="test failure")
    args = ["queue", "cancel", entry.queue_id, "--config", str(config_path)]
    if json_output:
        args.append("--json")
    assert main(args) == 1
    captured = capsys.readouterr()
    if json_output:
        # --json carries the failure as a document, never as a success payload.
        document = json.loads(captured.out)
        assert document["ok"] is False
        assert "already terminal" in document["error"]
    else:
        assert captured.out == ""
    assert "already terminal" in captured.err


@pytest.mark.parametrize("method", ["run", "run_once"])
def test_worker_body_timeout_is_not_duplicate_worker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], method: str
) -> None:
    class Worker(OrcaQueueWorker):
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

    worker = Worker(make_app_cfg(tmp_path, max_concurrent=1), "unused", max_concurrent=1)
    with pytest.raises(QueueLockTimeoutError, match="actual queue lock"):
        getattr(worker, method)()
    assert "already running" not in capsys.readouterr().err
