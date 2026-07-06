from __future__ import annotations

import json
import logging
import signal
from pathlib import Path

import pytest

from orca_auto.orca.orca_process import (
    ORCA_PROCESS_RECORD_FILE_NAME,
    recover_orphaned_orca_process,
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


def test_recover_orphaned_orca_process_ignores_reused_pid(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir, pid=1234, ticks=5678)

    def fail_killpg(_pgid: int, _signum: int) -> None:
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


def test_recover_reaps_live_orphan_whose_record_lacks_start_ticks(tmp_path: Path) -> None:
    # A record written without process_start_ticks (unreadable /proc at write
    # time) whose ORCA leader is still alive must be REAPED, not discarded as
    # PID reuse -- otherwise the orphan runs on beside the next retry.
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

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_no_ticks"),
        killpg_fn=fake_killpg,
        is_process_alive_fn=lambda _pid: True,
        process_start_ticks_fn=lambda _pid: None,
        sleep_fn=lambda _seconds: None,
    )

    assert recovered is True
    assert signals == [(1234, signal.SIGTERM)]
    assert not (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()
