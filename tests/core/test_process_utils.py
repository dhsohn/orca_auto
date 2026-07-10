from __future__ import annotations

import errno
import json
import signal
from pathlib import Path

import pytest

from orca_auto.core.utils import process as process_utils


def test_linux_boot_id_reads_nonempty_proc_value(tmp_path: Path) -> None:
    boot_id_path = tmp_path / "sys/kernel/random/boot_id"
    boot_id_path.parent.mkdir(parents=True)
    boot_id_path.write_text("  boot-123\n", encoding="utf-8")

    assert process_utils.linux_boot_id(proc_root=tmp_path) == "boot-123"


def test_linux_boot_id_returns_none_when_missing_or_blank(tmp_path: Path) -> None:
    assert process_utils.linux_boot_id(proc_root=tmp_path) is None
    boot_id_path = tmp_path / "sys/kernel/random/boot_id"
    boot_id_path.parent.mkdir(parents=True)
    boot_id_path.write_text(" \n", encoding="utf-8")
    assert process_utils.linux_boot_id(proc_root=tmp_path) is None


def test_stable_process_group_signal_uses_verified_pidfd_group_target() -> None:
    sent: list[tuple[int, int, int]] = []
    closed: list[int] = []
    deps = process_utils.StableProcessSignalDeps(
        open_process=lambda pid: 51 if pid == 123 else pytest.fail("unexpected pid"),
        read_identity=lambda fd: (123, 123, 456) if fd == 51 else pytest.fail("bad fd"),
        send_signal=lambda fd, signum, flags: sent.append((fd, signum, flags)),
        close=closed.append,
    )

    assert process_utils.signal_process_group_stable(
        123,
        123,
        456,
        signal.SIGTERM,
        deps=deps,
    )
    assert sent == [(51, signal.SIGTERM, process_utils.PIDFD_SIGNAL_PROCESS_GROUP)]
    assert closed == [51]


def test_stable_process_group_signal_falls_back_to_same_pidfd_leader() -> None:
    sent: list[tuple[int, int, int]] = []

    def send(fd: int, signum: int, flags: int) -> None:
        sent.append((fd, signum, flags))
        if flags == process_utils.PIDFD_SIGNAL_PROCESS_GROUP:
            raise OSError(errno.EINVAL, "group flag unsupported")

    assert not process_utils.signal_process_group_stable(
        123,
        123,
        456,
        signal.SIGKILL,
        deps=process_utils.StableProcessSignalDeps(
            open_process=lambda _pid: 52,
            read_identity=lambda _fd: (123, 123, 456),
            send_signal=send,
            close=lambda _fd: None,
        ),
    )
    assert sent == [
        (52, signal.SIGKILL, process_utils.PIDFD_SIGNAL_PROCESS_GROUP),
        (52, signal.SIGKILL, 0),
    ]


def test_stable_process_group_signal_rejects_replaced_identity_before_send() -> None:
    sent: list[tuple[int, int, int]] = []

    with pytest.raises(process_utils.StableProcessSignalError, match="identity changed"):
        process_utils.signal_process_group_stable(
            123,
            123,
            456,
            signal.SIGTERM,
            deps=process_utils.StableProcessSignalDeps(
                open_process=lambda _pid: 53,
                read_identity=lambda _fd: (123, 123, 999),
                send_signal=lambda fd, signum, flags: sent.append((fd, signum, flags)),
                close=lambda _fd: None,
            ),
        )
    assert sent == []


def test_boot_scoped_pid_payload_and_reader_reject_cross_boot_reuse(tmp_path: Path) -> None:
    payload = process_utils.current_pid_payload(
        now_fn=lambda: "2026-07-10T00:00:00+00:00",
        process_start_ticks_fn=lambda _pid: 456,
        pid_fn=lambda: 123,
        boot_id_fn=lambda: "boot-a",
    )
    assert payload["boot_id"] == "boot-a"

    pid_path = tmp_path / "worker.pid"
    pid_path.write_text(json.dumps(payload), encoding="utf-8")
    alive_calls: list[int] = []

    def record_alive_probe(pid: int) -> bool:
        alive_calls.append(pid)
        return True

    assert (
        process_utils.read_live_pid_file(
            pid_path,
            is_process_alive_fn=record_alive_probe,
            process_start_ticks_fn=lambda _pid: 456,
            boot_id_fn=lambda: "boot-b",
        )
        is None
    )
    assert alive_calls == []
    assert not pid_path.exists()


def test_pid_reader_keeps_legacy_and_unknown_current_boot_safe_biased(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.pid"
    legacy_path.write_text(
        json.dumps({"pid": 123, "process_start_ticks": 456}),
        encoding="utf-8",
    )
    assert (
        process_utils.read_live_pid_file(
            legacy_path,
            is_process_alive_fn=lambda _pid: True,
            process_start_ticks_fn=lambda _pid: 456,
            boot_id_fn=lambda: "boot-b",
        )
        == 123
    )

    scoped_path = tmp_path / "scoped.pid"
    scoped_path.write_text(
        json.dumps({"pid": 123, "process_start_ticks": 456, "boot_id": "boot-a"}),
        encoding="utf-8",
    )
    assert (
        process_utils.read_live_pid_file(
            scoped_path,
            is_process_alive_fn=lambda _pid: True,
            process_start_ticks_fn=lambda _pid: 456,
            boot_id_fn=lambda: None,
        )
        == 123
    )


def test_memory_limit_preexec_applies_address_space_limit() -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    process_utils.memory_limit_preexec(
        3,
        setrlimit_fn=lambda kind, limits: calls.append((kind, limits)),
        limit_resource=9,
    )()

    assert calls == [(9, (3 * 1024 * 1024 * 1024, 3 * 1024 * 1024 * 1024))]
