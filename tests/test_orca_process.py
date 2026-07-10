from __future__ import annotations

import json
import logging
import signal
import threading
import time
from pathlib import Path
from secrets import token_hex

import pytest

from orca_auto.orca import orca_process as orca_process_mod
from orca_auto.orca.orca_process import (
    ORCA_PROCESS_RECORD_FILE_NAME,
    OrcaProcessRecordCorruptError,
    recover_orphaned_orca_process,
    write_orca_process_record,
)

TEST_BOOT_ID = "test-boot-id"


def _wait_for_thread_event(
    event: threading.Event,
    thread: threading.Thread,
    *,
    description: str,
) -> None:
    for _attempt in range(1000):
        if event.is_set():
            return
        if not thread.is_alive():
            break
        time.sleep(0.01)
    pytest.fail(f"thread exited or stalled before {description}")


def _join_thread(thread: threading.Thread, *, description: str) -> None:
    for _attempt in range(1000):
        thread.join(0.01)
        if not thread.is_alive():
            return
    pytest.fail(f"thread did not finish after {description}")


def _write_process_record(reaction_dir: Path, *, pid: int = 1234, ticks: int = 5678) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "record_id": token_hex(8),
                "engine": "orca",
                "pid": pid,
                "pgid": pid,
                "process_start_ticks": ticks,
                "owner_boot_id": TEST_BOOT_ID,
                "process_boot_id": TEST_BOOT_ID,
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
        assert pgid == 1234
        assert signum == 0
        if not group_alive:
            raise ProcessLookupError

    def secure_signal(pid: int, pgid: int, ticks: int, signum: int) -> bool:
        nonlocal group_alive, pid_alive
        assert (pid, pgid, ticks) == (1234, 1234, 5678)
        signals.append((pgid, signum))
        if signum == signal.SIGTERM:
            group_alive = False
            pid_alive = False
        return True

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_orphaned_orca_process"),
        killpg_fn=fake_killpg,
        is_process_alive_fn=lambda _pid: pid_alive,
        process_start_ticks_fn=lambda _pid: 5678,
        boot_id_fn=lambda: TEST_BOOT_ID,
        secure_signal_fn=secure_signal,
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
        boot_id_fn=lambda: TEST_BOOT_ID,
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
    monkeypatch.setattr(
        orca_process_mod.process_utils,
        "linux_boot_id",
        lambda **_kwargs: TEST_BOOT_ID,
    )

    with pytest.raises(OrcaProcessRecordCorruptError, match="start ticks are unavailable"):
        write_orca_process_record(inp_path=inp, out_path=out, pid=1234)

    payload = json.loads((reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).read_text(encoding="utf-8"))
    assert payload["pid"] == 1234
    assert payload["owner_process_start_ticks"] == 777
    assert payload["owner_boot_id"] == TEST_BOOT_ID
    assert payload["process_boot_id"] == TEST_BOOT_ID
    assert "process_start_ticks" not in payload


def test_loaded_missing_ticks_cas_retains_concurrent_valid_record(
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
    monkeypatch.setattr(
        orca_process_mod.process_utils,
        "linux_boot_id",
        lambda **_kwargs: TEST_BOOT_ID,
    )
    with pytest.raises(OrcaProcessRecordCorruptError) as raised:
        write_orca_process_record(inp_path=inp, out_path=out, pid=1234)
    snapshot = orca_process_mod.orca_process_record_snapshot_from_exception(raised.value)
    assert snapshot is not None and "process_start_ticks" not in snapshot

    _write_process_record(reaction_dir, pid=1234, ticks=5678)
    replacement_path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    replacement_payload = json.loads(replacement_path.read_text(encoding="utf-8"))
    replacement_payload["record_id"] = snapshot["record_id"]
    replacement_path.write_text(json.dumps(replacement_payload), encoding="utf-8")
    assert not orca_process_mod.clear_orca_process_record_snapshot(
        reaction_dir,
        snapshot,
        pid=1234,
    )
    replacement = json.loads(replacement_path.read_text(encoding="utf-8"))
    assert replacement["process_start_ticks"] == 5678


def test_record_id_cas_retains_same_pid_ticks_boot_replacement(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir, pid=1234, ticks=5678)
    path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    original = json.loads(path.read_text(encoding="utf-8"))
    _write_process_record(reaction_dir, pid=1234, ticks=5678)
    replacement = json.loads(path.read_text(encoding="utf-8"))
    assert replacement["record_id"] != original["record_id"]

    assert not orca_process_mod.clear_orca_process_record_snapshot(
        reaction_dir,
        original,
        pid=1234,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["record_id"] == replacement["record_id"]


def test_clear_serializes_validation_and_unlink_against_replacement_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir, pid=1234, ticks=5678)
    path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    original = json.loads(path.read_text(encoding="utf-8"))
    inp = reaction_dir / "replacement.inp"
    out = reaction_dir / "replacement.out"

    monkeypatch.setattr(
        orca_process_mod.process_lock,
        "process_start_ticks",
        lambda _pid: 8765,
    )
    monkeypatch.setattr(
        orca_process_mod.process_lock,
        "current_process_start_ticks",
        lambda: 777,
    )
    monkeypatch.setattr(
        orca_process_mod.process_utils,
        "linux_boot_id",
        lambda **_kwargs: TEST_BOOT_ID,
    )

    validated = threading.Event()
    allow_clear = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    real_load = orca_process_mod.load_json_mapping_file
    failures: list[Exception] = []
    clear_results: list[bool] = []

    def load_then_pause(record_path: Path) -> dict[str, object] | None:
        payload = real_load(record_path)
        if threading.current_thread().name == "old-record-clearer":
            validated.set()
            allow_clear.wait()
        return payload

    monkeypatch.setattr(orca_process_mod, "load_json_mapping_file", load_then_pause)

    def clear_old_record() -> None:
        try:
            clear_results.append(
                orca_process_mod.clear_orca_process_record(
                    reaction_dir,
                    pid=1234,
                    process_start_ticks=5678,
                    process_boot_id=TEST_BOOT_ID,
                    record_id=original["record_id"],
                )
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted by parent
            failures.append(exc)

    def write_replacement_record() -> None:
        writer_started.set()
        try:
            write_orca_process_record(inp_path=inp, out_path=out, pid=9876)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted by parent
            failures.append(exc)
        finally:
            writer_finished.set()

    clear_thread = threading.Thread(target=clear_old_record, name="old-record-clearer")
    clear_thread.start()
    writer_thread: threading.Thread | None = None
    writer_bypassed_lock = False
    try:
        _wait_for_thread_event(
            validated,
            clear_thread,
            description="old record validation",
        )
        writer_thread = threading.Thread(
            target=write_replacement_record,
            name="replacement-writer",
        )
        writer_thread.start()
        _wait_for_thread_event(
            writer_started,
            writer_thread,
            description="replacement writer startup",
        )
        for _attempt in range(25):
            if writer_finished.is_set():
                writer_bypassed_lock = True
                break
            if not writer_thread.is_alive():
                writer_bypassed_lock = writer_finished.is_set()
                break
            time.sleep(0.01)
    finally:
        allow_clear.set()
        _join_thread(clear_thread, description="releasing old-record clear gate")
        if writer_thread is not None:
            _join_thread(writer_thread, description="releasing process-record lock")

    assert not writer_bypassed_lock, "replacement writer bypassed process-record lock"
    assert not clear_thread.is_alive()
    assert writer_thread is not None and not writer_thread.is_alive()
    assert failures == []
    assert clear_results == [True]
    replacement = json.loads(path.read_text(encoding="utf-8"))
    assert replacement["pid"] == 9876
    assert replacement["process_start_ticks"] == 8765
    assert replacement["record_id"] != original["record_id"]


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
            boot_id_fn=lambda: TEST_BOOT_ID,
            sleep_fn=lambda _seconds: None,
        )

    assert signals == []
    assert (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()


def test_recover_retains_leaderless_group_without_signalling(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir)
    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, signum: int) -> None:
        if signum == 0:
            return
        signals.append((pgid, signum))

    with pytest.raises(OrcaProcessRecordCorruptError, match="leader pid=1234 is gone"):
        recover_orphaned_orca_process(
            reaction_dir,
            logger=logging.getLogger("test_recover_leaderless_group"),
            killpg_fn=fake_killpg,
            is_process_alive_fn=lambda _pid: False,
            process_start_ticks_fn=lambda _pid: pytest.fail("dead leader has no ticks"),
            boot_id_fn=lambda: TEST_BOOT_ID,
        )

    assert signals == []
    assert (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()


def test_recover_clears_cross_boot_record_without_probing_or_signalling(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir)

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_cross_boot_record"),
        killpg_fn=lambda *_args: pytest.fail("cross-boot PGIDs must not be probed"),
        is_process_alive_fn=lambda _pid: pytest.fail("cross-boot PIDs must not be probed"),
        process_start_ticks_fn=lambda _pid: pytest.fail("cross-boot ticks must not be read"),
        boot_id_fn=lambda: "later-boot-id",
    )

    assert recovered is False
    assert not (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).exists()


def test_recover_rejects_bootless_legacy_record_without_signalling(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    record_path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    record_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "orca",
                "pid": 1234,
                "pgid": 1234,
                "process_start_ticks": 5678,
            }
        ),
        encoding="utf-8",
    )

    signals: list[int] = []

    def alive_group(_pgid: int, signum: int) -> None:
        if signum != 0:
            signals.append(signum)

    with pytest.raises(OrcaProcessRecordCorruptError, match="Legacy ORCA process record"):
        recover_orphaned_orca_process(
            reaction_dir,
            logger=logging.getLogger("test_recover_legacy_record"),
            killpg_fn=alive_group,
        )

    assert signals == []
    assert record_path.exists()


def test_recover_clears_bootless_legacy_record_when_group_is_absent(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    record_path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    record_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "orca",
                "pid": 1234,
                "pgid": 1234,
                "process_start_ticks": 5678,
            }
        ),
        encoding="utf-8",
    )

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_absent_legacy_record"),
        killpg_fn=lambda *_args: (_ for _ in ()).throw(ProcessLookupError),
    )

    assert recovered is False
    assert not record_path.exists()


def test_legacy_absent_group_clear_retains_boot_scoped_replacement(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    record_path = reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME
    record_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "orca",
                "pid": 1234,
                "pgid": 1234,
                "process_start_ticks": 5678,
            }
        ),
        encoding="utf-8",
    )

    def replace_then_report_absent(_pgid: int, signum: int) -> None:
        assert signum == 0
        _write_process_record(reaction_dir, pid=1234, ticks=5678)
        raise ProcessLookupError

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_legacy_replacement"),
        killpg_fn=replace_then_report_absent,
    )

    assert recovered is False
    replacement = json.loads(record_path.read_text(encoding="utf-8"))
    assert replacement["schema_version"] == 2
    assert replacement["process_boot_id"] == TEST_BOOT_ID


def test_absent_group_cleanup_does_not_delete_concurrent_replacement_record(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_process_record(reaction_dir, pid=1234, ticks=5678)

    def replace_record_then_report_absent(_pgid: int, signum: int) -> None:
        assert signum == 0
        _write_process_record(reaction_dir, pid=9876, ticks=8765)
        raise ProcessLookupError

    recovered = recover_orphaned_orca_process(
        reaction_dir,
        logger=logging.getLogger("test_recover_concurrent_replacement"),
        killpg_fn=replace_record_then_report_absent,
        boot_id_fn=lambda: TEST_BOOT_ID,
    )

    assert recovered is False
    replacement = json.loads(
        (reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME).read_text(encoding="utf-8")
    )
    assert replacement["pid"] == 9876
    assert replacement["process_start_ticks"] == 8765
