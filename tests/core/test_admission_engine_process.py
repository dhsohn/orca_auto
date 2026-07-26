from __future__ import annotations

import json
import multiprocessing
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core import admission
from orca_auto.core.admission import engine_process, store
from orca_auto.core.queue.cancellable import run_cancellable_engine_process
from orca_auto.core.queue.engine.child import (
    ChildWorkerEntrypointJob,
    await_parent_admission_handoff,
)
from orca_auto.orca.orca_runner import OrcaRunner


def _reserve_managed(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_pid: int = 101,
    owner_ticks: int = 1001,
) -> str:
    monkeypatch.setattr(store, "_process_start_ticks", lambda _pid: owner_ticks)
    token = admission.reserve_slot(
        root,
        10,
        source="test-engine",
        owner_pid=owner_pid,
        engine_process_state="idle",
    )
    assert token is not None
    return token


def _recover_absent_group(
    root: str,
    token: str,
    barrier: Any,
    results: Any,
) -> None:
    def missing(*_args: Any) -> None:
        raise ProcessLookupError

    barrier.wait()
    try:
        recovered = engine_process.recover_slot_engine_process(
            root,
            token,
            deps=engine_process.EngineProcessRecoveryDeps(
                killpg=missing,
                kill=missing,
                process_start_ticks=lambda _pid: None,
            ),
        )
    except BaseException as exc:  # noqa: BLE001  # pragma: no cover - returned to parent
        results.put(("error", repr(exc)))
    else:
        results.put(("ok", recovered))


def test_engine_slot_state_machine_and_release_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)

    pending = admission.prepare_slot_engine_process(tmp_path, token)
    assert pending is not None and pending.engine_process_state == "pending"
    with pytest.raises(ValueError, match="managed idle"):
        admission.prepare_slot_engine_process(tmp_path, token)
    with pytest.raises(RuntimeError, match="launch may be active"):
        admission.release_slot(tmp_path, token)

    active = admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    assert active is not None
    assert (
        active.engine_process_state,
        active.engine_pid,
        active.engine_pgid,
        active.engine_process_start_ticks,
    ) == ("active", 202, 202, 2002)
    assert active.owner_boot_id
    assert active.engine_process_boot_id == active.owner_boot_id
    persisted = json.loads((tmp_path / store.ADMISSION_FILE_NAME).read_text(encoding="utf-8"))[0]
    assert persisted["owner_boot_id"] == active.owner_boot_id
    assert persisted["engine_process_boot_id"] == active.owner_boot_id
    with pytest.raises(RuntimeError, match="launch may be active"):
        admission.release_slot(tmp_path, token)

    idle = admission.clear_slot_engine_process(
        tmp_path,
        token,
        expected_pid=202,
        expected_process_start_ticks=2002,
    )
    assert idle is not None and idle.engine_process_state == "idle"
    assert admission.release_slot(tmp_path, token) is True


@pytest.mark.parametrize(
    "payload",
    [
        {"token": None, "owner_pid": 1, "process_start_ticks": 1},
        {"token": 123, "owner_pid": 1, "process_start_ticks": 1},
        {
            "token": "slot",
            "owner_pid": 1,
            "process_start_ticks": 1,
            "engine_process_state": "active",
            "engine_pid": True,
            "engine_pgid": True,
            "engine_process_start_ticks": True,
        },
        {
            "token": "slot",
            "owner_pid": True,
            "process_start_ticks": 1,
            "engine_process_state": "idle",
        },
        {
            "token": "slot",
            "owner_pid": 1,
            "process_start_ticks": 1,
            "owner_boot_id": "boot-a",
            "engine_process_state": "active",
            "engine_pid": 2,
            "engine_pgid": 2,
            "engine_process_start_ticks": 2,
        },
        {
            "token": "slot",
            "owner_pid": 1,
            "process_start_ticks": 1,
            "engine_process_state": "active",
            "engine_pid": 2,
            "engine_pgid": 2,
            "engine_process_start_ticks": 2,
            "engine_process_boot_id": "boot-a",
        },
    ],
)
def test_admission_schema_rejects_unsafe_identity_coercions(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    (tmp_path / store.ADMISSION_FILE_NAME).write_text(
        json.dumps([payload]),
        encoding="utf-8",
    )

    with pytest.raises(store.AdmissionStoreCorruptError):
        admission.list_all_slots(tmp_path)


def test_active_recovery_terminates_group_and_clears_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    group_alive = True
    signals: list[int] = []

    def killpg(_pgid: int, signum: int) -> None:
        assert signum == 0
        if not group_alive:
            raise ProcessLookupError

    def secure_signal(pid: int, pgid: int, ticks: int, signum: int) -> bool:
        nonlocal group_alive
        assert (pid, pgid, ticks) == (202, 202, 2002)
        signals.append(signum)
        group_alive = False
        return True

    recovered = engine_process.recover_slot_engine_process(
        tmp_path,
        token,
        deps=engine_process.EngineProcessRecoveryDeps(
            killpg=killpg,
            kill=lambda _pid, _sig: None,
            secure_signal=secure_signal,
            process_start_ticks=lambda _pid: 2002,
            sleep=lambda _seconds: None,
        ),
    )

    assert recovered is True
    assert signals == [signal.SIGTERM]
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "idle"


def test_active_recovery_clears_reused_pid_without_signalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    signals: list[int] = []

    def killpg(_pgid: int, signum: int) -> None:
        if signum != 0:
            signals.append(signum)

    assert (
        engine_process.recover_slot_engine_process(
            tmp_path,
            token,
            deps=engine_process.EngineProcessRecoveryDeps(
                killpg=killpg,
                kill=lambda _pid, _sig: None,
                process_start_ticks=lambda _pid: 9999,
            ),
        )
        is False
    )
    assert signals == []
    assert admission.get_slot(tmp_path, token).engine_process_state == "idle"  # type: ignore[union-attr]


def test_active_recovery_retains_unknown_live_pid_without_signalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    signals: list[int] = []

    def killpg(_pgid: int, signum: int) -> None:
        if signum != 0:
            signals.append(signum)

    with pytest.raises(engine_process.EngineProcessRecordError, match="start ticks unavailable"):
        engine_process.recover_slot_engine_process(
            tmp_path,
            token,
            deps=engine_process.EngineProcessRecoveryDeps(
                killpg=killpg,
                kill=lambda _pid, _sig: None,
                process_start_ticks=lambda _pid: None,
            ),
        )

    assert signals == []
    assert admission.get_slot(tmp_path, token).engine_process_state == "active"  # type: ignore[union-attr]


def test_active_recovery_retains_leaderless_group_without_signalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    signals: list[int] = []

    def killpg(_pgid: int, signum: int) -> None:
        if signum != 0:
            signals.append(signum)

    def missing_leader(_pid: int, _signum: int) -> None:
        raise ProcessLookupError

    with pytest.raises(engine_process.EngineProcessRecordError, match="leader pid=202 is gone"):
        engine_process.recover_slot_engine_process(
            tmp_path,
            token,
            deps=engine_process.EngineProcessRecoveryDeps(
                killpg=killpg,
                kill=missing_leader,
                process_start_ticks=lambda _pid: pytest.fail("dead leader has no ticks"),
            ),
        )

    assert signals == []
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "active"


def test_active_recovery_clears_cross_boot_identity_without_signalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    signals: list[int] = []

    def killpg(_pgid: int, signum: int) -> None:
        if signum != 0:
            signals.append(signum)

    recovered = engine_process.recover_slot_engine_process(
        tmp_path,
        token,
        deps=engine_process.EngineProcessRecoveryDeps(
            killpg=killpg,
            kill=lambda *_args: pytest.fail("cross-boot PID must not be probed"),
            process_start_ticks=lambda _pid: pytest.fail("cross-boot ticks must not be read"),
            boot_id=lambda: "a-later-boot",
        ),
    )

    assert recovered is False
    assert signals == []
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "idle"


def test_global_recovery_clears_cross_boot_pending_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    path = tmp_path / store.ADMISSION_FILE_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[0]["owner_boot_id"] = "earlier-boot"
    path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = engine_process.recover_orphaned_engine_slots(
        tmp_path,
        source="test-engine",
        deps=engine_process.EngineProcessRecoveryDeps(
            kill=lambda *_args: pytest.fail("cross-boot owner PID must not be probed"),
            killpg=lambda *_args: pytest.fail("pending slots have no group to probe"),
            boot_id=lambda: "current-boot",
        ),
    )

    assert recovered == 1
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "idle"


def test_cross_boot_pending_cas_retains_concurrent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    path = tmp_path / store.ADMISSION_FILE_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[0]["owner_boot_id"] = "earlier-boot"
    path.write_text(json.dumps(payload), encoding="utf-8")
    real_complete = store.complete_slot_engine_process

    def replace_then_complete(root: Path, slot_token: str, **kwargs: Any) -> Any:
        replacement = json.loads(path.read_text(encoding="utf-8"))
        replacement[0]["owner_pid"] = 303
        replacement[0]["process_start_ticks"] = 3003
        replacement[0]["owner_boot_id"] = "current-boot"
        path.write_text(json.dumps(replacement), encoding="utf-8")
        return real_complete(root, slot_token, **kwargs)

    monkeypatch.setattr(engine_process, "complete_slot_engine_process", replace_then_complete)

    recovered = engine_process.recover_orphaned_engine_slots(
        tmp_path,
        source="test-engine",
        deps=engine_process.EngineProcessRecoveryDeps(
            kill=lambda *_args: pytest.fail("cross-boot owner PID must not be probed"),
            killpg=lambda *_args: pytest.fail("pending slots have no group to probe"),
            boot_id=lambda: "current-boot",
        ),
        strict=False,
    )

    assert recovered == 0
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None
    assert (slot.owner_pid, slot.process_start_ticks, slot.owner_boot_id) == (
        303,
        3003,
        "current-boot",
    )
    assert slot.engine_process_state == "pending"


def test_global_recovery_handles_active_before_retaining_dead_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "_process_start_ticks", lambda pid: pid * 10)
    monkeypatch.setattr(store.os, "kill", lambda _pid, _sig: None)
    active_token = admission.reserve_slot(
        tmp_path,
        10,
        source="test-engine",
        owner_pid=101,
        engine_process_state="idle",
    )
    pending_token = admission.reserve_slot(
        tmp_path,
        10,
        source="test-engine",
        owner_pid=102,
        engine_process_state="idle",
    )
    assert active_token is not None and pending_token is not None
    admission.prepare_slot_engine_process(tmp_path, active_token)
    admission.set_slot_engine_process(
        tmp_path,
        active_token,
        pid=202,
        pgid=202,
        process_start_ticks=2020,
    )
    admission.prepare_slot_engine_process(tmp_path, pending_token)
    group_alive = True

    def kill(pid: int, _sig: int) -> None:
        if pid in {101, 102}:
            raise ProcessLookupError
        assert pid == 202

    def killpg(_pgid: int, signum: int) -> None:
        assert signum == 0
        if not group_alive:
            raise ProcessLookupError

    def secure_signal(_pid: int, _pgid: int, _ticks: int, _signum: int) -> bool:
        nonlocal group_alive
        group_alive = False
        return True

    deps = engine_process.EngineProcessRecoveryDeps(
        killpg=killpg,
        kill=kill,
        secure_signal=secure_signal,
        process_start_ticks=lambda pid: 2020 if pid == 202 else None,
        sleep=lambda _seconds: None,
    )
    assert (
        engine_process.recover_orphaned_engine_slots(
            tmp_path,
            source="test-engine",
            deps=deps,
            strict=False,
        )
        == 1
    )
    assert admission.get_slot(tmp_path, active_token).engine_process_state == "idle"  # type: ignore[union-attr]
    assert admission.get_slot(tmp_path, pending_token).engine_process_state == "pending"  # type: ignore[union-attr]
    with pytest.raises(engine_process.EngineProcessRecordError, match="pending engine launch"):
        engine_process.recover_orphaned_engine_slots(
            tmp_path,
            source="test-engine",
            deps=deps,
            strict=True,
        )


def test_concurrent_recovery_clear_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_recover_absent_group,
            args=(str(tmp_path), token, barrier, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    observed = sorted(results.get(timeout=1) for _ in workers)
    assert all(kind == "ok" for kind, _value in observed)
    assert admission.get_slot(tmp_path, token).engine_process_state == "idle"  # type: ignore[union-attr]


def test_registrar_publication_failure_cleans_process_and_pending_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        engine_process.process_utils,
        "process_start_ticks",
        lambda *_args, **_kwargs: None,
    )

    class Process:
        pid = 202
        exited = False

        def poll(self) -> int | None:
            return 1 if self.exited else None

    process = Process()

    def terminate(_process: Process) -> bool:
        process.exited = True
        return True

    def start_job() -> SimpleNamespace:
        # CREST/xTB prepare immediately inside start_job, just before Popen;
        # the outer cancellable preparation flag therefore remains false.
        admission.build_slot_engine_process_preparer(tmp_path, token)()
        return SimpleNamespace(process=process)

    result = run_cancellable_engine_process(
        start_job=start_job,
        register_running_job=admission.build_slot_engine_process_registrar(tmp_path, token),
        finalize_job=lambda *_args, **_kwargs: pytest.fail("finalizer must not run"),
        terminate_process=terminate,
        build_failure_result=lambda exc: type(exc).__name__,
    )

    assert result == "EngineProcessRecordError"
    assert process.exited is True
    assert admission.get_slot(tmp_path, token).engine_process_state == "idle"  # type: ignore[union-attr]


def test_prepare_compensates_pending_record_after_post_replace_save_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    original_save = store._save_slots
    save_calls = 0

    def save_then_fail_once(root: Path, slots: list[store.AdmissionSlot]) -> None:
        nonlocal save_calls
        save_calls += 1
        original_save(root, slots)
        if save_calls == 1:
            raise OSError("directory fsync failed after replace")

    monkeypatch.setattr(store, "_save_slots", save_then_fail_once)

    with pytest.raises(engine_process.EngineProcessRecordError, match="Cannot prepare"):
        admission.build_slot_engine_process_preparer(tmp_path, token)()

    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "idle"
    assert save_calls == 2


def test_prepare_compensation_never_discards_visible_active_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    original_save = store._save_slots

    def publish_active_then_fail(root: Path, slots: list[store.AdmissionSlot]) -> None:
        pending = next(slot for slot in slots if slot.token == token)
        active = replace(
            pending,
            engine_process_state="active",
            engine_pid=202,
            engine_pgid=202,
            engine_process_start_ticks=2002,
            engine_process_boot_id=pending.owner_boot_id,
        )
        original_save(
            root,
            [active if slot.token == token else slot for slot in slots],
        )
        raise OSError("save outcome was ambiguous")

    monkeypatch.setattr(store, "_save_slots", publish_active_then_fail)

    with pytest.raises(engine_process.EngineProcessRecordError, match="Cannot prepare"):
        admission.build_slot_engine_process_preparer(tmp_path, token)()

    slot = admission.get_slot(tmp_path, token)
    assert slot is not None
    assert (
        slot.engine_process_state,
        slot.engine_pid,
        slot.engine_pgid,
        slot.engine_process_start_ticks,
        slot.engine_process_boot_id,
    ) == ("active", 202, 202, 2002, slot.owner_boot_id)


def test_existing_slot_can_opt_into_managed_engine_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "_process_start_ticks", lambda _pid: 1001)
    monkeypatch.setattr(store, "_linux_boot_id", lambda: "test-boot")
    monkeypatch.setattr(store.os, "kill", lambda _pid, _sig: None)
    token = admission.reserve_slot(tmp_path, 1, source="generic", owner_pid=101)
    assert token is not None

    updated = admission.update_slot_metadata(
        tmp_path,
        token,
        engine_process_state="idle",
    )

    assert updated is not None
    assert updated.process_start_ticks == 1001
    assert updated.owner_boot_id == "test-boot"
    assert updated.engine_process_state == "idle"


@pytest.mark.parametrize(
    ("raised", "expected_state"),
    [(OSError("popen failed"), "idle"), (KeyboardInterrupt(), "pending")],
)
def test_orca_popen_failure_clears_only_unambiguous_pending_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    expected_state: str,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    executable = tmp_path / "fake-orca"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    runner = OrcaRunner(str(executable))
    runner.set_running_job_registrar(
        admission.build_slot_engine_process_registrar(tmp_path, token),
        prepare=admission.build_slot_engine_process_preparer(tmp_path, token),
    )
    inp = tmp_path / "job.inp"
    inp.write_text("! SP\n", encoding="utf-8")

    def fail_popen(*_args: Any, **_kwargs: Any) -> Any:
        raise raised

    monkeypatch.setattr("orca_auto.orca.orca_runner.subprocess.Popen", fail_popen)
    with pytest.raises(type(raised)):
        runner.run(inp)

    assert admission.get_slot(tmp_path, token).engine_process_state == expected_state  # type: ignore[union-attr]


def test_parent_handoff_waits_until_slot_owner_matches_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = cast(
        ChildWorkerEntrypointJob,
        SimpleNamespace(admission_root=lambda: tmp_path),
    )
    slots = iter(
        [
            SimpleNamespace(owner_pid=99),
            SimpleNamespace(owner_pid=123),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr("orca_auto.core.queue.engine.child.get_slot", lambda *_args: next(slots))
    monkeypatch.setattr("orca_auto.core.queue.engine.child.os.getpid", lambda: 123)

    assert await_parent_admission_handoff(
        job,
        "slot",
        timeout_seconds=1,
        monotonic_fn=lambda: 0,
        sleep_fn=sleeps.append,
    )
    assert sleeps == [0.01]


def test_global_dead_owner_recovery_escalates_term_to_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    group_alive = True
    signals: list[int] = []
    clock = iter([0.0, 3.0, 3.0])

    def kill(pid: int, _signum: int) -> None:
        if pid == 101:
            raise ProcessLookupError
        assert pid == 202

    def killpg(pgid: int, signum: int) -> None:
        assert pgid == 202
        assert signum == 0
        if not group_alive:
            raise ProcessLookupError

    def secure_signal(pid: int, pgid: int, ticks: int, signum: int) -> bool:
        nonlocal group_alive
        assert (pid, pgid, ticks) == (202, 202, 2002)
        signals.append(signum)
        if signum == signal.SIGKILL:
            group_alive = False
        return True

    recovered = engine_process.recover_orphaned_engine_slots(
        tmp_path,
        source="test-engine",
        deps=engine_process.EngineProcessRecoveryDeps(
            killpg=killpg,
            kill=kill,
            secure_signal=secure_signal,
            process_start_ticks=lambda pid: 2002 if pid == 202 else None,
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
        ),
    )

    assert recovered == 1
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "idle"


def test_registrar_publish_store_failure_retains_pending_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    monkeypatch.setattr(
        engine_process,
        "set_slot_engine_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(
        engine_process.EngineProcessRecordError,
        match="Cannot publish engine process identity",
    ):
        engine_process.register_slot_engine_process(
            tmp_path,
            token,
            SimpleNamespace(process=SimpleNamespace(pid=202)),
            deps=engine_process.EngineProcessRecoveryDeps(
                process_start_ticks=lambda _pid: 2002,
            ),
        )

    slot = admission.get_slot(tmp_path, token)
    assert slot is not None and slot.engine_process_state == "pending"
    with pytest.raises(RuntimeError, match="launch may be active"):
        admission.release_slot(tmp_path, token)


def test_registrar_clear_failure_retains_active_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _reserve_managed(tmp_path, monkeypatch)
    admission.prepare_slot_engine_process(tmp_path, token)
    admission.set_slot_engine_process(
        tmp_path,
        token,
        pid=202,
        pgid=202,
        process_start_ticks=2002,
    )
    clear_calls = 0

    def fail_clear(*_args: Any, **_kwargs: Any) -> None:
        nonlocal clear_calls
        clear_calls += 1
        return None

    def missing_group(_pgid: int, _signum: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(engine_process, "clear_slot_engine_process", fail_clear)

    with pytest.raises(
        engine_process.EngineProcessRecordError,
        match="changed while clearing",
    ):
        engine_process.register_slot_engine_process(
            tmp_path,
            token,
            None,
            deps=engine_process.EngineProcessRecoveryDeps(killpg=missing_group),
        )

    assert clear_calls == 2
    slot = admission.get_slot(tmp_path, token)
    assert slot is not None
    assert (
        slot.engine_process_state,
        slot.engine_pid,
        slot.engine_pgid,
        slot.engine_process_start_ticks,
    ) == ("active", 202, 202, 2002)
    with pytest.raises(RuntimeError, match="launch may be active"):
        admission.release_slot(tmp_path, token)
