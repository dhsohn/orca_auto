from __future__ import annotations

import errno
import json
import os
import signal
from pathlib import Path

import pytest

from orca_auto.core.utils import process as process_utils


def test_is_process_alive_handles_absent_permission_and_unknown_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not process_utils.is_process_alive(0)
    monkeypatch.setattr(
        os, "kill", lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError())
    )
    assert not process_utils.is_process_alive(123)
    monkeypatch.setattr(os, "kill", lambda _pid, _signal: (_ for _ in ()).throw(PermissionError()))
    assert process_utils.is_process_alive(123)
    monkeypatch.setattr(
        os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(OSError(errno.EBUSY, "busy")),
    )
    assert process_utils.is_process_alive(123)


def _proc_stat_text(start_ticks: str) -> str:
    fields = ["S"] + [str(index) for index in range(1, 19)] + [start_ticks]
    return f"1234 (python) {' '.join(fields)}"


def test_process_start_ticks_handles_parse_failures_and_success(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    assert process_utils.process_start_ticks(999, proc_root=proc_root) is None

    pid = 1234
    stat_file = proc_root / str(pid) / "stat"
    stat_file.parent.mkdir(parents=True)
    for text in (
        "",
        "1234 no-right-paren",
        "1234 (python) S 1 2 3",
        _proc_stat_text("not-an-int"),
        _proc_stat_text("0"),
    ):
        stat_file.write_text(text, encoding="utf-8")
        assert process_utils.process_start_ticks(pid, proc_root=proc_root) is None

    stat_file.write_text(_proc_stat_text("54321"), encoding="utf-8")
    assert process_utils.process_start_ticks(pid, proc_root=proc_root) == 54321


@pytest.mark.parametrize(
    ("stat_text", "field", "ticks"),
    [
        ("", None, None),
        ("1 (proc) " + " ".join(str(i) for i in range(19)), None, None),
        ("1 (proc) " + " ".join(["0"] * 19 + ["bad"]), "bad", None),
        ("1 (a) b) " + " ".join(str(i) for i in range(1, 21)), "20", 20),
    ],
)
def test_stat_parser_takes_field_22_after_the_last_paren(
    stat_text: str, field: str | None, ticks: int | None
) -> None:
    assert process_utils.stat_starttime_field(stat_text) == field
    assert process_utils.parse_stat_start_ticks(stat_text) == ticks


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


def _stable_signal_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fd: int,
    identity: tuple[int, int, int],
    send: object,
    closed: list[int] | None = None,
) -> None:
    monkeypatch.setattr(
        process_utils,
        "_open_proc_pid_directory",
        lambda pid: fd if pid == 123 else pytest.fail("unexpected pid"),
    )
    monkeypatch.setattr(
        process_utils,
        "_read_proc_identity_from_directory_fd",
        lambda opened: identity if opened == fd else pytest.fail("bad fd"),
    )
    monkeypatch.setattr(process_utils, "_pidfd_send_signal", send)
    monkeypatch.setattr(
        process_utils.os, "close", (closed.append if closed is not None else lambda _fd: None)
    )


def test_stable_process_group_signal_uses_verified_pidfd_group_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int, int]] = []
    closed: list[int] = []
    _stable_signal_host(
        monkeypatch,
        fd=51,
        identity=(123, 123, 456),
        send=lambda fd, signum, flags: sent.append((fd, signum, flags)),
        closed=closed,
    )

    assert process_utils.signal_process_group_stable(123, 123, 456, signal.SIGTERM)
    assert sent == [(51, signal.SIGTERM, process_utils.PIDFD_SIGNAL_PROCESS_GROUP)]
    assert closed == [51]


def test_stable_process_group_signal_falls_back_to_same_pidfd_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int, int]] = []

    def send(fd: int, signum: int, flags: int) -> None:
        sent.append((fd, signum, flags))
        if flags == process_utils.PIDFD_SIGNAL_PROCESS_GROUP:
            raise OSError(errno.EINVAL, "group flag unsupported")

    _stable_signal_host(monkeypatch, fd=52, identity=(123, 123, 456), send=send)

    assert not process_utils.signal_process_group_stable(123, 123, 456, signal.SIGKILL)
    assert sent == [
        (52, signal.SIGKILL, process_utils.PIDFD_SIGNAL_PROCESS_GROUP),
        (52, signal.SIGKILL, 0),
    ]


def test_stable_process_group_signal_rejects_replaced_identity_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int, int]] = []
    _stable_signal_host(
        monkeypatch,
        fd=53,
        identity=(123, 123, 999),
        send=lambda fd, signum, flags: sent.append((fd, signum, flags)),
    )

    with pytest.raises(process_utils.StableProcessSignalError, match="identity changed"):
        process_utils.signal_process_group_stable(123, 123, 456, signal.SIGTERM)
    assert sent == []


def _host_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    boot_id: str | None,
    ticks: int | None = 456,
    alive: object = None,
) -> None:
    monkeypatch.setattr(process_utils, "linux_boot_id", lambda **_kwargs: boot_id)
    monkeypatch.setattr(process_utils, "process_start_ticks", lambda _pid, **_kwargs: ticks)
    monkeypatch.setattr(process_utils, "is_process_alive", alive or (lambda _pid: True))


def test_boot_scoped_pid_payload_and_reader_reject_cross_boot_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_identity(monkeypatch, boot_id="boot-a")
    monkeypatch.setattr(process_utils.os, "getpid", lambda: 123)
    monkeypatch.setattr(process_utils, "now_utc_iso", lambda: "2026-07-10T00:00:00+00:00")
    payload = process_utils.current_pid_payload()
    assert payload == {
        "pid": 123,
        "started_at": "2026-07-10T00:00:00+00:00",
        "process_start_ticks": 456,
        "boot_id": "boot-a",
    }

    pid_path = tmp_path / "worker.pid"
    pid_path.write_text(json.dumps(payload), encoding="utf-8")
    alive_calls: list[int] = []

    def record_alive_probe(pid: int) -> bool:
        alive_calls.append(pid)
        return True

    _host_identity(monkeypatch, boot_id="boot-b", alive=record_alive_probe)
    assert process_utils.read_live_pid_file(pid_path) is None
    assert alive_calls == []
    assert not pid_path.exists()


def test_pid_reader_rejects_incomplete_or_unverifiable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_identity(monkeypatch, boot_id="boot-b")
    incomplete_path = tmp_path / "incomplete.pid"
    incomplete_path.write_text(
        json.dumps({"pid": 123, "process_start_ticks": 456}),
        encoding="utf-8",
    )
    assert process_utils.read_live_pid_file(incomplete_path) is None
    assert not incomplete_path.exists()

    _host_identity(monkeypatch, boot_id=None)
    scoped_path = tmp_path / "scoped.pid"
    scoped_path.write_text(
        json.dumps({"pid": 123, "process_start_ticks": 456, "boot_id": "boot-a"}),
        encoding="utf-8",
    )
    assert process_utils.read_live_pid_file(scoped_path) is None
    assert not scoped_path.exists()
