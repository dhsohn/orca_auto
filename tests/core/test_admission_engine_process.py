from __future__ import annotations

import json
import logging
import multiprocessing
import signal
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
from orca_auto.orca.commands import run_inp_execution
from orca_auto.orca.orca_process import (
    OrcaProcessRecordCorruptError,
    OrcaProcessRecoveryError,
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
        nonlocal group_alive
        if signum == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append(signum)
        group_alive = False

    recovered = engine_process.recover_slot_engine_process(
        tmp_path,
        token,
        deps=engine_process.EngineProcessRecoveryDeps(
            killpg=killpg,
            kill=lambda _pid, _sig: None,
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
        if pid in {101, 102, 202}:
            raise ProcessLookupError

    def killpg(_pgid: int, signum: int) -> None:
        nonlocal group_alive
        if signum == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        group_alive = False

    deps = engine_process.EngineProcessRecoveryDeps(
        killpg=killpg,
        kill=kill,
        process_start_ticks=lambda _pid: None,
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
    runner = OrcaRunner("/opt/orca/orca")
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


def test_unsafe_legacy_recovery_error_is_not_converted_to_exit_code(tmp_path: Path) -> None:
    released: list[tuple[Path, str | None]] = []
    context = SimpleNamespace(
        selected_inp=tmp_path / "job.inp",
        admission_root=tmp_path,
        reservation_token="slot",
    )
    deps = SimpleNamespace(
        execution=SimpleNamespace(
            _resolve_execution_context=lambda *_args, **_kwargs: context,
            _execute_locked_run=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OrcaProcessRecordCorruptError("unsafe recovery")
            ),
            _release_reservation_if_needed=lambda root, token: released.append((root, token)),
        ),
        statuses=SimpleNamespace(AdmissionLimitReachedError=RuntimeError),
    )

    with pytest.raises(OrcaProcessRecoveryError, match="unsafe recovery"):
        run_inp_execution.cmd_run_inp_execute(
            SimpleNamespace(),
            runner_cls=OrcaRunner,
            cfg=None,
            reaction_dir=None,
            selected_inp=None,
            reservation_token=None,
            admission_app_name=None,
            admission_task_id=None,
            deps=deps,
            logger=logging.getLogger("test.unsafe_legacy_recovery"),
        )

    assert released == []


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
        nonlocal group_alive
        assert pgid == 202
        if signum == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append(signum)
        if signum == signal.SIGKILL:
            group_alive = False

    recovered = engine_process.recover_orphaned_engine_slots(
        tmp_path,
        source="test-engine",
        deps=engine_process.EngineProcessRecoveryDeps(
            killpg=killpg,
            kill=kill,
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
