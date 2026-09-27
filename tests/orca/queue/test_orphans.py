"""``orca_auto.orca.queue.orphans``: reading the worker pid file."""

from __future__ import annotations

from pathlib import Path

from orca_auto.orca.queue.orphans import read_worker_pid

# ---------------------------------------------------------------------------
# read_worker_pid
# ---------------------------------------------------------------------------


def test_no_pid_file(tmp_path: Path) -> None:
    assert read_worker_pid(tmp_path) is None


def test_stale_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "queue_worker.pid"
    pid_path.write_text("999999999")  # non-existent pid
    assert read_worker_pid(tmp_path) is None
    # PID file should be cleaned up
    assert not pid_path.exists()


def test_invalid_pid_content(tmp_path: Path) -> None:
    (tmp_path / "queue_worker.pid").write_text("not_a_number")
    assert read_worker_pid(tmp_path) is None
