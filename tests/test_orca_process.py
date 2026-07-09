from __future__ import annotations

import json
import logging
import signal
from pathlib import Path

import pytest

from orca_auto.orca import orca_process as orca_process_mod
from orca_auto.orca.orca_process import (
    ORCA_PROCESS_RECORD_FILE_NAME,
    OrcaProcessRecordCorruptError,
    recover_orphaned_orca_process,
    write_orca_process_record,
)


def _write_process_record(reaction_dir: Path, *, pid: int = 1234, ticks: int = 5678) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "orca",
                "pid": pid,
                "pgid": pid,
                "process_start_ticks": ticks,
                "inp_path": str(reaction_dir / "calc.inp"),
                "out_path": str(reaction_dir / "calc.out"),
            }
        ),
        encoding="utf-8",
    )


def test_recover_orphaned_orca_process_terminates_recorded_group(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir)
    group_alive = True
    pid_alive = True
    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, signum: int) -> None:
        nonlocal group_alive, pid_alive
        if signum == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append((pgid, signum))
        if signum == signal.SIGTERM:
            group_alive = False
            pid_alive = False

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_orphaned_orca_process"),
        killpg_fn=fake_killpg,
        is_process_alive_fn=lambda _pid: pid_alive,
        process_start_ticks_fn=lambda _pid: 5678,
        sleep_fn=lambda _seconds: None,
    )

    assert recovered is True
    assert signals == [(1234, signal.SIGTERM)]
    assert not (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()


def test_recover_orphaned_orca_process_allows_missing_record(tmp_path: Path) -> None:
    recovered = recover_orphaned_orca_process(
        tmp_path / "rxn",
        logger=logging.getLogger("test_recover_missing_record"),
        killpg_fn=lambda *_args: pytest.fail("missing records must not probe process groups"),
    )

    assert recovered is False


@pytest.mark.parametrize(
    "record_text",
    [
        "{not-json",
        "[]",
        json.dumps({"schema_version": 1, "engine": "orca"}),
        json.dumps({"schema_version": 1, "engine": "other", "pid": 1234, "pgid": 1234}),
    ],
)
def test_recover_orphaned_orca_process_rejects_corrupt_record(
    tmp_path: Path,
    record_text: str,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    record_path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    record_path.write_text(record_text, encoding="utf-8")

    with pytest.raises(OrcaProcessRecordCorruptError):
        recover_orphaned_orca_process(
            reaction_dir,
            logger=logging.getLogger("test_recover_corrupt_record"),
            killpg_fn=lambda *_args: pytest.fail("corrupt records must fail before signalling"),
        )

    assert record_path.exists()


def test_recover_orphaned_orca_process_rejects_unreadable_record(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    record_path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    record_path.mkdir(parents=True)

    with pytest.raises(OrcaProcessRecordCorruptError, match="cannot be read"):
        recover_orphaned_orca_process(
            reaction_dir,
            logger=logging.getLogger("test_recover_unreadable_record"),
        )


def test_recover_orphaned_orca_process_ignores_reused_pid(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir, pid=1234, ticks=5678)

    def fail_killpg(_pgid: int, signum: int) -> None:
        if signum != 0:
            pytest.fail("PID-reused records must not be signalled")

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_orphaned_orca_process"),
        killpg_fn=fail_killpg,
        is_process_alive_fn=lambda _pid: True,
        process_start_ticks_fn=lambda _pid: 9999,
        sleep_fn=lambda _seconds: None,
    )

    assert recovered is False
    assert not (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()


def test_process_group_is_alive_probes_the_group() -> None:
    from orca_auto.orca.orca_process import process_group_is_alive

    seen: list[tuple[int, int]] = []

    def alive_killpg(pgid: int, signum: int) -> None:
        seen.append((pgid, signum))

    assert process_group_is_alive(4321, killpg_fn=alive_killpg) is True
    assert seen == [(4321, 0)]

    def dead_killpg(_pgid: int, _signum: int) -> None:
        raise ProcessLookupError

    assert process_group_is_alive(4321, killpg_fn=dead_killpg) is False


def test_recover_rejects_live_record_without_start_ticks(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir(parents=True, exist_ok=True)
    (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).write_text(
        json.dumps({"schema_version": 1, "engine": "orca", "pid": 1234, "pgid": 1234}),
        encoding="utf-8",
    )
    group_alive = True
    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, signum: int) -> None:
        nonlocal group_alive
        if signum == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append((pgid, signum))
        if signum == signal.SIGTERM:
            group_alive = False

    with pytest.raises(OrcaProcessRecordCorruptError, match="Invalid ORCA process record"):
        recover_orphaned_orca_process(
            reaction_dir,
            logger=logging.getLogger("test_recover_no_ticks"),
            killpg_fn=fake_killpg,
            is_process_alive_fn=lambda _pid: True,
            process_start_ticks_fn=lambda _pid: None,
            sleep_fn=lambda _seconds: None,
        )

    assert signals == []
    assert (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()


def test_writer_aborts_and_persists_fail_closed_record_when_ticks_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    inp = reaction_dir / "calc.inp"
    out = reaction_dir / "calc.out"
    monkeypatch.setattr(
        orca_process_mod.process_lock,
        "process_start_ticks",
        lambda _pid: None,
    )
    monkeypatch.setattr(
        orca_process_mod.process_lock,
        "current_process_start_ticks",
        lambda: 777,
    )

    with pytest.raises(OrcaProcessRecordCorruptError, match="start ticks are unavailable"):
        write_orca_process_record(inp_path=inp, out_path=out, pid=1234)

    payload = json.loads((reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).read_text(encoding="utf-8"))
    assert payload["pid"] == 1234
    assert payload["owner_process_start_ticks"] == 777
    assert "process_start_ticks" not in payload


def test_recover_fails_closed_when_live_leader_ticks_are_unreadable(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir, pid=1234, ticks=5678)
    group_alive = True
    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, signum: int) -> None:
        nonlocal group_alive
        if signum == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append((pgid, signum))

    with pytest.raises(OrcaProcessRecordCorruptError, match="Cannot verify"):
        recover_orphaned_orca_process(
            reaction_dir,
            logger=logging.getLogger("test_recover_ticks_vanish"),
            killpg_fn=fake_killpg,
            is_process_alive_fn=lambda _pid: True,
            process_start_ticks_fn=lambda _pid: None,
            sleep_fn=lambda _seconds: None,
        )

    assert signals == []
    assert (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()
