from __future__ import annotations

import fcntl
import os
from multiprocessing import get_context
from pathlib import Path

import pytest

from orca_auto.core.utils.lock import file_lock, file_lock_at, held_file_lock_payload


def _hold_lock_until_released(lock_path: str, ready, release) -> None:
    with file_lock(Path(lock_path), timeout_seconds=1.0):
        ready.set()
        release.wait()


def _hold_lock_then_crash(lock_path: str, ready) -> None:
    with file_lock(Path(lock_path), timeout_seconds=1.0):
        ready.set()
        os._exit(0)


def test_file_lock_writes_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "nested" / "resource.lock"
    monkeypatch.setattr("orca_auto.core.utils.lock.os.getpid", lambda: 4321)
    monkeypatch.setattr(
        "orca_auto.core.utils.lock.now_utc_iso", lambda: "2026-04-19T12:34:56+00:00"
    )

    with file_lock(lock_path):
        contents = lock_path.read_text(encoding="utf-8")

    assert lock_path.parent.exists()
    assert contents == "pid=4321\nacquired_at=2026-04-19T12:34:56+00:00\n"


def test_file_lock_writes_custom_payload_and_reports_it_only_while_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"

    with file_lock(lock_path, payload='{"pid":4321}'):
        assert held_file_lock_payload(lock_path) == '{"pid":4321}\n'

    assert held_file_lock_payload(lock_path) is None
    assert lock_path.read_text(encoding="utf-8") == '{"pid":4321}\n'


def test_held_file_lock_payload_ignores_unlocked_stale_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "resource.lock"
    lock_path.write_text('{"pid":999999}', encoding="utf-8")

    assert held_file_lock_payload(lock_path) is None


def test_file_lock_times_out_when_lock_is_held(tmp_path: Path) -> None:
    ctx = get_context("fork")
    lock_path = tmp_path / "held.lock"
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_lock_until_released,
        args=(str(lock_path), ready, release),
    )
    process.start()

    try:
        acquired = False
        for _ in range(100):
            acquired = ready.wait(0.1)
            if acquired or process.exitcode is not None:
                break
        assert acquired, "child process never acquired the lock"

        with pytest.raises(TimeoutError, match=r"Timed out acquiring lock: .*held\.lock"):
            with file_lock(lock_path, timeout_seconds=0.05):
                pass
    finally:
        release.set()
        for _ in range(100):
            process.join(timeout=0.1)
            if not process.is_alive():
                break
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_file_lock_is_reacquired_after_owner_process_crashes(tmp_path: Path) -> None:
    ctx = get_context("fork")
    lock_path = tmp_path / "crashed.lock"
    ready = ctx.Event()
    process = ctx.Process(target=_hold_lock_then_crash, args=(str(lock_path), ready))
    process.start()
    assert ready.wait(5), "child process never acquired the lock"
    process.join(timeout=5)
    assert process.exitcode == 0

    with file_lock(lock_path, timeout_seconds=0.1):
        assert held_file_lock_payload(lock_path) is not None


def test_file_lock_ignores_unlock_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "resource.lock"
    calls: list[int] = []

    def fake_flock(fd: int, flags: int) -> None:
        calls.append(flags)
        if flags & fcntl.LOCK_UN:
            raise OSError("unlock failed")

    monkeypatch.setattr("orca_auto.core.utils.lock.fcntl.flock", fake_flock)

    with file_lock(lock_path):
        pass

    assert fcntl.LOCK_UN in calls


def test_file_lock_at_uses_pinned_directory_descriptor(tmp_path: Path) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with file_lock_at(
            directory_fd,
            "resource.lock",
            display_path=tmp_path / "resource.lock",
        ):
            contents = (tmp_path / "resource.lock").read_text(encoding="utf-8")
    finally:
        os.close(directory_fd)

    assert f"pid={os.getpid()}\n" in contents


def test_file_lock_at_does_not_fsync_its_diagnostic_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    fsynced: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr("orca_auto.core.utils.lock.os.fsync", recording_fsync)
    lock_path = tmp_path / "resource.lock"
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with file_lock_at(
            directory_fd,
            "resource.lock",
            display_path=lock_path,
            payload='{"pid":4321}',
        ):
            # The payload is written and flushed for other processes ...
            held = held_file_lock_payload(lock_path)
            calls_while_held = list(fsynced)
    finally:
        os.close(directory_fd)

    # ... but never fsynced: the flock dies with its owner, so the diagnostic
    # bytes have no reader after a crash and durability would be an fsync for
    # nothing on every acquisition.
    assert held == '{"pid":4321}\n'
    assert calls_while_held == []
    assert fsynced == []
    assert lock_path.read_text(encoding="utf-8") == '{"pid":4321}\n'


@pytest.mark.parametrize("lock_name", ["", ".", "..", "nested/resource.lock"])
def test_file_lock_at_rejects_non_plain_names(tmp_path: Path, lock_name: str) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="one plain filename"):
            with file_lock_at(directory_fd, lock_name):
                pass
    finally:
        os.close(directory_fd)
