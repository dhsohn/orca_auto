from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.artifacts import RUN_REPORT_JSON_FILE
from orca_auto.core.queue.engine.worker_execution import WorkerShutdownRequested
from orca_auto.flow.engines.crest import execution as worker_execution
from orca_auto.flow.engines.crest import terminal as crest_terminal
from orca_auto.flow.engines.crest.runner import CrestRunResult
from orca_auto.flow.engines.crest.state import load_state
from tests.engine_artifact_helpers import (
    artifact_payload,
)
from tests.engine_artifact_helpers import (
    engine_payload as _engine_payload,
)
from tests.engine_artifact_helpers import (
    input_payload as _input_payload,
)
from tests.engine_artifact_helpers import (
    job as _job,
)
from tests.engine_artifact_helpers import (
    recovery as _recovery,
)
from tests.engine_artifact_helpers import (
    resources as _resources,
)
from tests.engine_artifact_helpers import (
    status as _status,
)
from tests.engine_artifact_helpers import (
    timestamps as _timestamps,
)
from tests.execution_snapshot_helpers import stage_execution_snapshot


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(tmp_path)),
        resources=SimpleNamespace(max_cores_per_task=4, max_memory_gb_per_task=16),
    )


def _entry(
    job_dir: Path | str,
    selected_xyz: Path | str,
    *,
    task_id: str = "job-001",
    queue_id: str = "queue-001",
    app_name: str = "orca_auto",
    started_at: str | None = "2026-04-19T00:00:00+00:00",
    mode: str = "standard",
    molecule_key: str = "",
) -> SimpleNamespace:
    job_path = Path(job_dir)
    selected_path = Path(selected_xyz)
    resource_request = {"max_cores": 4, "max_memory_gb": 16}
    execution_snapshot: dict[str, Any] | None = None
    selected_snapshot = selected_path
    if job_path.is_dir() and selected_path.is_file():
        selected_snapshot, execution_snapshot = stage_execution_snapshot(
            job_path,
            selected_path,
            engine="crest",
            manifest={"mode": mode},
            resource_request=resource_request,
            identity={"mode": mode, "molecule_key": molecule_key},
        )
    metadata = {
        "job_dir": str(job_path),
        "selected_input_xyz": str(selected_snapshot),
        "mode": mode,
        "molecule_key": molecule_key,
        "resource_request": resource_request,
    }
    if execution_snapshot is not None:
        metadata["execution_snapshot"] = execution_snapshot
    return SimpleNamespace(
        task_id=task_id,
        queue_id=queue_id,
        app_name=app_name,
        task_kind="crest_conformer_search",
        engine="crest",
        priority=10,
        enqueued_at="2026-04-18T23:59:00+00:00",
        started_at=started_at,
        metadata=metadata,
    )


def _result(
    job_dir: Path,
    selected_xyz: Path,
    *,
    status: str = "completed",
    reason: str = "completed",
    mode: str = "standard",
    exit_code: int = 0,
    retained_names: tuple[str, ...] = (),
) -> CrestRunResult:
    stdout_log = job_dir / "crest.stdout.log"
    stderr_log = job_dir / "crest.stderr.log"
    stdout_log.write_text("stdout\n", encoding="utf-8")
    stderr_log.write_text("stderr\n", encoding="utf-8")

    retained_paths: list[str] = []
    for name in retained_names:
        path = job_dir / name
        path.write_text("1\nretained\nH 0.0 0.0 0.0\n", encoding="utf-8")
        retained_paths.append(str(path.resolve()))

    return CrestRunResult(
        status=status,
        reason=reason,
        command=("crest", selected_xyz.name, "--T", "4"),
        exit_code=exit_code,
        started_at="2026-04-19T00:00:00+00:00",
        finished_at="2026-04-19T00:05:00+00:00",
        stdout_log=str(stdout_log.resolve()),
        stderr_log=str(stderr_log.resolve()),
        selected_input_xyz=str(selected_xyz.resolve()),
        mode=mode,
        retained_conformer_count=len(retained_paths),
        retained_conformer_paths=tuple(retained_paths),
        manifest_path=str((job_dir / "crest_job.yaml").resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 16},
        resource_actual={"assigned_cores": 4, "memory_limit_gb": 16},
    )


def _context(
    entry: SimpleNamespace,
    job_dir: Path,
    selected_xyz: Path,
    *,
    molecule_key: str = "mol-001",
    mode: str = "standard",
) -> worker_execution.ExecutionContext:
    return worker_execution.ExecutionContext(
        entry=entry,
        job_dir=job_dir.resolve(),
        selected_xyz=selected_xyz.resolve(),
        molecule_key=molecule_key,
        mode=mode,
        resource_request={"max_cores": 4, "max_memory_gb": 16},
    )


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


def _notify_ok(*args: Any, **kwargs: Any) -> bool:
    return True


def _commit_terminal(*args: Any, **kwargs: Any) -> bool:
    before_update_fn = kwargs.get("before_update_fn")
    if before_update_fn is not None:
        before_update_fn()
    return True


def _record_committed_terminal(
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]],
    *args: Any,
    **kwargs: Any,
) -> bool:
    before_update_fn = kwargs.pop("before_update_fn", None)
    if before_update_fn is not None:
        before_update_fn()
    calls.append((args, kwargs))
    return True


def _dependencies(**overrides: Callable[..., Any]) -> worker_execution.WorkerExecutionDependencies:
    defaults: dict[str, Any] = {
        "now_utc_iso": lambda: "2026-04-19T09:15:00+00:00",
        "get_cancel_requested": lambda *args, **kwargs: False,
        "start_crest_job": _noop,
        "finalize_crest_job": _noop,
        "terminate_process": _noop,
        "wait_for_cancellable_process": worker_execution._queue_execution.wait_for_cancellable_process,
        "sleep": worker_execution.time.sleep,
        "cancel_check_interval_seconds": worker_execution.CANCEL_CHECK_INTERVAL_SECONDS,
        "write_running_state": _noop,
        "write_execution_artifacts": _noop,
        "mark_completed": _commit_terminal,
        "mark_cancelled": _commit_terminal,
        "mark_failed": _commit_terminal,
        "upsert_job_record": _noop,
        "notify_job_started": _notify_ok,
        "notify_job_finished": _notify_ok,
    }
    defaults.update(overrides)
    return worker_execution.build_worker_execution_dependencies(
        timing=worker_execution.WorkerTimingDependencies(
            now_utc_iso=defaults["now_utc_iso"],
        ),
        queue=worker_execution.WorkerQueueDependencies(
            get_cancel_requested=defaults["get_cancel_requested"],
            mark_completed=defaults["mark_completed"],
            mark_cancelled=defaults["mark_cancelled"],
            mark_failed=defaults["mark_failed"],
        ),
        runner=worker_execution.WorkerRunnerDependencies(
            start_crest_job=defaults["start_crest_job"],
            finalize_crest_job=defaults["finalize_crest_job"],
            terminate_process=defaults["terminate_process"],
            wait_for_cancellable_process=defaults["wait_for_cancellable_process"],
            sleep=defaults["sleep"],
            cancel_check_interval_seconds=defaults["cancel_check_interval_seconds"],
        ),
        artifacts=worker_execution.WorkerArtifactDependencies(
            write_running_state=defaults["write_running_state"],
            write_execution_artifacts=defaults["write_execution_artifacts"],
        ),
        tracking=worker_execution.WorkerTrackingDependencies(
            upsert_job_record=defaults["upsert_job_record"],
            notify_job_started=defaults["notify_job_started"],
            notify_job_finished=defaults["notify_job_finished"],
        ),
    )


class FakeProcess:
    def __init__(self, *poll_values: int | None) -> None:
        self._poll_values = list(poll_values) or [None]
        self.pid = 4242

    def poll(self) -> int | None:
        if len(self._poll_values) > 1:
            return self._poll_values.pop(0)
        return self._poll_values[0]

    def exit(self, return_code: int = -15) -> None:
        self._poll_values = [return_code]


@dataclass
class ProcessDequeuedEntrySpy:
    running: Any
    result: CrestRunResult
    sleeps: list[int] = field(default_factory=list)
    molecule_key_calls: list[tuple[SimpleNamespace, Path, Path]] = field(default_factory=list)
    cancel_checks: list[tuple[str, str]] = field(default_factory=list)
    finalize_kwargs: list[dict[str, Any]] = field(default_factory=list)
    terminate_calls: list[Any] = field(default_factory=list)
    running_state_calls: list[str] = field(default_factory=list)
    artifact_results: list[CrestRunResult] = field(default_factory=list)
    mark_completed_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    mark_cancelled_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    mark_failed_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    upsert_calls: list[dict[str, Any]] = field(default_factory=list)
    started_notifications: list[dict[str, Any]] = field(default_factory=list)
    finished_notifications: list[dict[str, Any]] = field(default_factory=list)

    def sleep(self, seconds: int) -> None:
        self.sleeps.append(seconds)

    def molecule_key(
        self,
        actual_entry: SimpleNamespace,
        actual_selected_xyz: Path,
        actual_job_dir: Path,
    ) -> str:
        self.molecule_key_calls.append((actual_entry, actual_selected_xyz, actual_job_dir))
        return "derived-key"

    def finalize(self, running_job: Any, **kwargs: Any) -> CrestRunResult:
        self.finalize_kwargs.append(kwargs)
        assert running_job is self.running
        return self.result

    def get_cancel_requested(
        self,
        root: str,
        queue_id: str,
        **_kwargs: object,
    ) -> bool:
        self.cancel_checks.append((root, queue_id))
        return False

    def notify_started(self, cfg: Any, **kwargs: Any) -> bool:
        self.started_notifications.append(kwargs)
        return True

    def notify_finished(self, cfg: Any, **kwargs: Any) -> bool:
        self.finished_notifications.append(kwargs)
        return True

    def dependencies(self) -> worker_execution.WorkerExecutionDependencies:
        return _dependencies(
            get_cancel_requested=self.get_cancel_requested,
            start_crest_job=lambda cfg, *, job_dir, selected_xyz, execution_snapshot: self.running,
            finalize_crest_job=self.finalize,
            terminate_process=lambda proc: self.terminate_calls.append(proc),
            write_running_state=lambda cfg, actual_entry: self.running_state_calls.append(
                actual_entry.task_id
            ),
            write_execution_artifacts=lambda actual_entry, actual_result: (
                self.artifact_results.append(actual_result)
            ),
            mark_completed=lambda *args, **kwargs: _record_committed_terminal(
                self.mark_completed_calls, *args, **kwargs
            ),
            mark_cancelled=lambda *args, **kwargs: _record_committed_terminal(
                self.mark_cancelled_calls, *args, **kwargs
            ),
            mark_failed=lambda *args, **kwargs: _record_committed_terminal(
                self.mark_failed_calls, *args, **kwargs
            ),
            upsert_job_record=lambda cfg, **kwargs: self.upsert_calls.append(kwargs),
            notify_job_started=self.notify_started,
            notify_job_finished=self.notify_finished,
        )


def test_write_execution_artifacts_returns_early_without_job_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected_xyz = tmp_path / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry("   ", selected_xyz)
    result = _result(tmp_path, selected_xyz)

    monkeypatch.setattr(
        worker_execution,
        "write_state",
        lambda *args, **kwargs: pytest.fail("unexpected state write"),
    )

    worker_execution._write_execution_artifacts(entry, result)


def test_write_execution_artifacts_writes_retained_paths_to_state_only(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, molecule_key="mol-42")
    result = _result(
        job_dir,
        selected_xyz,
        reason="ok",
        retained_names=("crest_conformers.xyz", "crest_best.xyz"),
    )

    worker_execution._write_execution_artifacts(entry, result)

    state_payload = load_state(job_dir)
    assert state_payload is not None
    assert _status(state_payload)["state"] == "completed"
    assert _engine_payload(state_payload)["retained_conformer_count"] == 2
    assert _engine_payload(state_payload)["retained_conformer_paths"] == list(
        result.retained_conformer_paths
    )
    assert _job(state_payload)["queue_id"] == entry.queue_id
    assert _engine_payload(state_payload)["molecule_key"] == "mol-42"
    assert _engine_payload(state_payload)["command"] == list(result.command)
    assert not (job_dir / RUN_REPORT_JSON_FILE).exists()


def test_write_running_state_returns_early_without_job_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    entry = _entry("   ", "")

    monkeypatch.setattr(
        worker_execution,
        "write_state",
        lambda *args, **kwargs: pytest.fail("unexpected state write"),
    )

    worker_execution._write_running_state(cfg, entry)


def test_write_running_state_writes_running_payload_with_fallback_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, started_at=None, mode="nci", molecule_key="mol-42")
    captured: dict[str, Any] = {}
    timestamps = iter(
        [
            "2026-04-19T08:00:00+00:00",
            "2026-04-19T08:00:01+00:00",
        ]
    )

    monkeypatch.setattr("orca_auto.core.utils.now_utc_iso", lambda: next(timestamps))
    monkeypatch.setattr(
        worker_execution,
        "write_state",
        lambda actual_job_dir, payload: captured.update(job_dir=actual_job_dir, payload=payload),
    )

    worker_execution._write_running_state(cfg, entry)

    assert captured["job_dir"] == job_dir.resolve()
    payload = captured["payload"]
    assert payload["schema_version"] == 1
    assert payload["engine"] == "crest"
    assert _job(payload)["id"] == entry.task_id
    assert _job(payload)["dir"] == str(job_dir.resolve())
    assert _input_payload(payload)["selected_xyz_path"] == entry.metadata["selected_input_xyz"]
    assert _engine_payload(payload)["molecule_key"] == "mol-42"
    assert _engine_payload(payload)["mode"] == "nci"
    assert _status(payload)["state"] == "running"
    assert _status(payload)["reason"] == ""
    assert _timestamps(payload)["started_at"] == "2026-04-19T08:00:00+00:00"
    assert _timestamps(payload)["updated_at"] == "2026-04-19T08:00:01+00:00"
    assert _timestamps(payload)["created_at"] == "2026-04-19T08:00:00+00:00"
    assert _resources(payload)["request"] == {"max_cores": 4, "max_memory_gb": 16}
    assert _resources(payload)["actual"] == {"max_cores": 4, "max_memory_gb": 16}
    assert _recovery(payload)["pending"] is False
    assert _recovery(payload)["count"] == 0
    assert _recovery(payload)["resumed"] is False


def test_write_running_state_marks_resumed_when_recovery_pending_state_exists(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, mode="nci", molecule_key="mol-42")
    worker_execution.write_state(
        job_dir,
        artifact_payload(
            engine="crest",
            job_id=entry.task_id,
            job_dir=str(job_dir.resolve()),
            status="queued",
            reason="worker_shutdown",
            primary_path=str(selected_xyz.resolve()),
            selected_xyz_path=str(selected_xyz.resolve()),
            created_at="2026-04-19T07:59:00+00:00",
            updated_at="2026-04-19T07:59:00+00:00",
            recovery_pending=True,
            recovery_reason="worker_shutdown",
            recovery_count=2,
            engine_payload={
                "molecule_key": "mol-42",
                "mode": "nci",
            },
        ),
    )

    worker_execution._write_running_state(cfg, entry)

    payload = load_state(job_dir)
    assert payload is not None
    assert _status(payload)["state"] == "running"
    assert _status(payload)["reason"] == "worker_shutdown"
    assert _timestamps(payload)["created_at"] == "2026-04-19T07:59:00+00:00"
    assert _recovery(payload)["pending"] is False
    assert _recovery(payload)["reason"] == "worker_shutdown"
    assert _recovery(payload)["count"] == 2
    assert _recovery(payload)["resumed"] is True


def test_molecule_key_prefers_metadata_and_falls_back_to_selected_xyz(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "Selected Input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")

    assert (
        worker_execution._molecule_key(
            _entry(job_dir, selected_xyz, molecule_key=" fixed-key "),
            selected_xyz,
            job_dir,
        )
        == "fixed-key"
    )
    assert (
        worker_execution._molecule_key(
            _entry(job_dir, selected_xyz, molecule_key=" "),
            selected_xyz,
            job_dir,
        )
        == "selected_input"
    )


def test_terminate_process_returns_immediately_when_process_has_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 5555

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls: list[float] = []

        def poll(self) -> int | None:
            return 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

        def wait(self, timeout: float) -> None:
            self.wait_calls.append(timeout)

    proc = ExitedProcess()
    monkeypatch.setattr(
        worker_execution.os,
        "killpg",
        lambda _pid, signum: (
            (_ for _ in ()).throw(ProcessLookupError())
            if signum == 0
            else pytest.fail("only the process-group existence probe should run")
        ),
    )

    worker_execution._terminate_process(cast(Any, proc))

    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert proc.wait_calls == []


def test_terminate_process_falls_back_to_proc_methods_and_escalates_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningProcess:
        pid = 7777

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls: list[float] = []

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

        def wait(self, timeout: float) -> None:
            self.wait_calls.append(timeout)
            raise subprocess.TimeoutExpired(cmd="crest", timeout=timeout)

    proc = RunningProcess()
    killpg_calls: list[tuple[int, signal.Signals]] = []

    def fake_killpg(pid: int, sig: signal.Signals) -> None:
        killpg_calls.append((pid, sig))
        raise PermissionError("denied")

    monkeypatch.setattr(worker_execution.os, "killpg", fake_killpg)

    worker_execution._terminate_process(cast(Any, proc))

    assert killpg_calls == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert proc.wait_calls == pytest.approx([10, 5], rel=1e-4)


def test_terminate_process_swallows_proc_method_errors_after_killpg_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FlakyProcess:
        pid = 8888

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls: list[float] = []

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.terminate_calls += 1
            raise RuntimeError("terminate failed")

        def kill(self) -> None:
            self.kill_calls += 1
            raise RuntimeError("kill failed")

        def wait(self, timeout: float) -> None:
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired(cmd="crest", timeout=timeout)

    proc = FlakyProcess()
    monkeypatch.setattr(
        worker_execution.os,
        "killpg",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProcessLookupError()),
    )

    worker_execution._terminate_process(cast(Any, proc))

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert proc.wait_calls == pytest.approx([10, 5], rel=1e-4)


def test_sync_job_tracking_never_organizes_for_crest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, molecule_key="fixed-key")
    context = _context(entry, job_dir, selected_xyz, molecule_key="fixed-key")
    result = _result(job_dir, selected_xyz, reason="ok")
    upsert_calls: list[dict[str, Any]] = []

    deps = _dependencies(
        upsert_job_record=lambda cfg, **kwargs: upsert_calls.append(kwargs),
    )

    sync_result = crest_terminal.sync_job_tracking(
        cfg,
        context,
        result,
        tracking_deps=deps.tracking,
    )

    assert sync_result is None
    assert len(upsert_calls) == 1
    assert upsert_calls[0]["job_id"] == entry.task_id
    assert upsert_calls[0]["job_dir"] == job_dir.resolve()
    assert upsert_calls[0]["molecule_key"] == "fixed-key"


def test_process_dequeued_entry_uses_context_dependency_group(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry("ignored-job-dir", "ignored-selected-xyz", mode="ignored")
    _selected_snapshot, execution_snapshot = stage_execution_snapshot(
        job_dir,
        selected_xyz,
        engine="crest",
        manifest={"mode": "nci"},
        resource_request={"max_cores": 2, "max_memory_gb": 6},
        identity={"mode": "nci", "molecule_key": "ctx-key"},
    )
    entry.metadata["execution_snapshot"] = execution_snapshot
    running = SimpleNamespace(process=FakeProcess(None, 0))
    result = _result(job_dir, selected_xyz, reason="ok", mode="nci")
    sleeps: list[float] = []
    upsert_calls: list[dict[str, Any]] = []
    started_notifications: list[dict[str, Any]] = []

    def notify_started(cfg: Any, **kwargs: Any) -> bool:
        del cfg
        started_notifications.append(kwargs)
        return True

    deps = worker_execution.build_worker_execution_dependencies(
        timing=worker_execution.WorkerTimingDependencies(
            now_utc_iso=lambda: "2026-04-19T09:15:00+00:00",
        ),
        queue=worker_execution.WorkerQueueDependencies(
            get_cancel_requested=lambda *args, **kwargs: False,
            mark_completed=_noop,
            mark_cancelled=_noop,
            mark_failed=_noop,
        ),
        context=worker_execution.WorkerContextDependencies(
            job_dir=lambda _entry: job_dir.resolve(),
            selected_xyz=lambda _entry: selected_xyz.resolve(),
            molecule_key=lambda _entry, _selected_xyz, _job_dir: "ctx-key",
            mode=lambda _entry: "nci",
            entry_resource_request=lambda _cfg, _entry: {
                "max_cores": 2,
                "max_memory_gb": 6,
            },
        ),
        runner=worker_execution.WorkerRunnerDependencies(
            start_crest_job=lambda _cfg, *, job_dir, selected_xyz, execution_snapshot: running,
            finalize_crest_job=lambda actual_running, **kwargs: result,
            terminate_process=lambda _process: True,
            wait_for_cancellable_process=(
                worker_execution._queue_execution.wait_for_cancellable_process
            ),
            sleep=lambda seconds: sleeps.append(seconds),
            cancel_check_interval_seconds=worker_execution.CANCEL_CHECK_INTERVAL_SECONDS,
        ),
        artifacts=worker_execution.WorkerArtifactDependencies(
            write_running_state=_noop,
            write_execution_artifacts=_noop,
        ),
        tracking=worker_execution.WorkerTrackingDependencies(
            upsert_job_record=lambda cfg, **kwargs: upsert_calls.append(kwargs),
            notify_job_started=notify_started,
            notify_job_finished=_notify_ok,
        ),
    )

    outcome = worker_execution.process_dequeued_entry(cfg, entry, dependencies=deps)

    assert outcome.job_dir == job_dir.resolve()
    assert outcome.selected_xyz == selected_xyz.resolve()
    assert outcome.molecule_key == "ctx-key"
    assert sleeps == [worker_execution.CANCEL_CHECK_INTERVAL_SECONDS]
    assert upsert_calls[0]["mode"] == "nci"
    assert upsert_calls[0]["resource_request"] == {"max_cores": 2, "max_memory_gb": 6}
    assert started_notifications[0]["mode"] == "nci"


def test_run_worker_child_job_uses_dependency_config_and_admission_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1")
    released: list[tuple[str, str]] = []
    deps = worker_execution.build_worker_execution_dependencies(
        config=worker_execution.WorkerConfigDependencies(
            load_config=lambda path: cfg,
            queue_entry_by_id=lambda root, queue_id: entry,
        ),
        admission=worker_execution.WorkerAdmissionDependencies(
            activate_reserved_slot=lambda *args, **kwargs: object(),
            release_slot=lambda root, token: released.append((str(root), token)),
        ),
    )
    captured: dict[str, Any] = {}

    def fake_run_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        assert kwargs["load_config_fn"]("/tmp/orca_auto.yaml") is cfg
        assert kwargs["find_queue_entry_fn"](tmp_path / "queue", "queue-1") is entry
        assert kwargs["dependencies_fn"]() is deps
        return 0

    monkeypatch.setattr(
        worker_execution,
        "run_engine_worker_child_job",
        fake_run_worker_child_job,
    )

    rc = worker_execution.run_worker_child_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        dependencies=deps,
    )

    assert rc == 0
    assert captured["queue_id"] == "queue-1"


def test_process_dequeued_entry_polls_sleeps_and_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, molecule_key="derived-key")
    proc = FakeProcess(None, 0)
    running = SimpleNamespace(process=proc)
    result = _result(job_dir, selected_xyz, reason="ok")
    spy = ProcessDequeuedEntrySpy(running=running, result=result)

    monkeypatch.setattr(worker_execution.time, "sleep", spy.sleep)

    outcome = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        molecule_key_resolver=spy.molecule_key,
        dependencies=spy.dependencies(),
    )

    assert outcome.result == result
    assert outcome.job_dir == job_dir.resolve()
    assert outcome.selected_xyz == selected_xyz.resolve()
    assert outcome.molecule_key == "derived-key"
    assert spy.molecule_key_calls == [(entry, selected_xyz.resolve(), job_dir.resolve())]
    assert spy.cancel_checks == [(cfg.runtime.allowed_root, entry.queue_id)]
    assert spy.sleeps == [worker_execution.CANCEL_CHECK_INTERVAL_SECONDS]
    assert spy.finalize_kwargs == [{}]
    assert spy.terminate_calls == []
    assert spy.running_state_calls == [entry.task_id]
    assert len(spy.artifact_results) == 1
    [artifact_result] = spy.artifact_results
    assert artifact_result.status == result.status
    assert artifact_result.reason == result.reason
    assert set(artifact_result.output_identities) == {
        result.stdout_log,
        result.stderr_log,
    }
    assert [call["status"] for call in spy.upsert_calls] == ["running", "completed"]
    assert len(spy.mark_completed_calls) == 1
    assert spy.mark_completed_calls[0][0] == (cfg.runtime.allowed_root, entry.queue_id)
    assert spy.mark_completed_calls[0][1]["metadata_update"] == {
        "retained_conformer_count": result.retained_conformer_count,
    }
    assert spy.mark_cancelled_calls == []
    assert spy.mark_failed_calls == []
    assert spy.started_notifications[0]["selected_xyz"] == selected_xyz.resolve()
    assert spy.finished_notifications[0]["status"] == "completed"
    assert spy.finished_notifications[0]["resource_request"] == {
        "max_cores": 4,
        "max_memory_gb": 16,
    }


def test_process_dequeued_entry_terminates_and_forces_cancelled_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, molecule_key="cancel-key")
    proc = FakeProcess(None)
    running = SimpleNamespace(process=proc)
    result = _result(
        job_dir, selected_xyz, status="cancelled", reason="cancel_requested", exit_code=-15
    )

    sleeps: list[int] = []
    finalize_kwargs: list[dict[str, Any]] = []
    terminate_calls: list[FakeProcess] = []
    mark_cancelled_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    finished_notifications: list[dict[str, Any]] = []

    monkeypatch.setattr(worker_execution.time, "sleep", lambda seconds: sleeps.append(seconds))

    def fake_finalize(running_job: Any, **kwargs: Any) -> CrestRunResult:
        finalize_kwargs.append(kwargs)
        assert running_job is running
        return result

    def fake_notify_finished(cfg: Any, **kwargs: Any) -> bool:
        finished_notifications.append(kwargs)
        return True

    def terminate(actual_proc: FakeProcess) -> bool:
        terminate_calls.append(actual_proc)
        actual_proc.exit()
        return True

    deps = _dependencies(
        get_cancel_requested=lambda *args, **kwargs: True,
        start_crest_job=lambda cfg, *, job_dir, selected_xyz, execution_snapshot: running,
        finalize_crest_job=fake_finalize,
        terminate_process=terminate,
        mark_cancelled=lambda *args, **kwargs: _record_committed_terminal(
            mark_cancelled_calls, *args, **kwargs
        ),
        notify_job_finished=fake_notify_finished,
    )

    outcome = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        molecule_key_resolver=lambda entry, selected_xyz, job_dir: "cancel-key",
        dependencies=deps,
    )

    assert outcome.result == result
    assert sleeps == []
    assert terminate_calls == [proc]
    assert finalize_kwargs == [
        {
            "forced_status": "cancelled",
            "forced_reason": "cancel_requested",
        }
    ]
    assert len(mark_cancelled_calls) == 1
    assert mark_cancelled_calls[0][0] == (cfg.runtime.allowed_root, entry.queue_id)
    assert mark_cancelled_calls[0][1]["error"] == "cancel_requested"
    assert finished_notifications[0]["status"] == "cancelled"
    assert finished_notifications[0]["reason"] == "cancel_requested"


def test_process_dequeued_entry_builds_failed_result_when_runner_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    manifest_path = job_dir / "crest_job.yaml"
    manifest_path.write_text("mode: standard\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, started_at=None, molecule_key="failure-key")
    failure_time = "2026-04-19T11:30:00+00:00"

    sleeps: list[int] = []
    artifact_results: list[CrestRunResult] = []
    mark_failed_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    upsert_calls: list[dict[str, Any]] = []
    finished_notifications: list[dict[str, Any]] = []

    monkeypatch.setattr(worker_execution.time, "sleep", lambda seconds: sleeps.append(seconds))

    def fake_notify_finished(cfg: Any, **kwargs: Any) -> bool:
        finished_notifications.append(kwargs)
        return True

    deps = _dependencies(
        now_utc_iso=lambda: failure_time,
        start_crest_job=lambda cfg, *, job_dir, selected_xyz, execution_snapshot: (
            _ for _ in ()
        ).throw(RuntimeError("boom")),
        finalize_crest_job=lambda *args, **kwargs: pytest.fail("finalize should not run"),
        get_cancel_requested=lambda *args, **kwargs: pytest.fail("cancel should not be checked"),
        terminate_process=lambda *args, **kwargs: pytest.fail("terminate should not run"),
        write_execution_artifacts=lambda actual_entry, actual_result: artifact_results.append(
            actual_result
        ),
        mark_failed=lambda *args, **kwargs: _record_committed_terminal(
            mark_failed_calls, *args, **kwargs
        ),
        upsert_job_record=lambda cfg, **kwargs: upsert_calls.append(kwargs),
        notify_job_finished=fake_notify_finished,
    )

    outcome = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        molecule_key_resolver=lambda entry, selected_xyz, job_dir: "failure-key",
        dependencies=deps,
    )

    result = outcome.result
    assert result.status == "failed"
    assert result.reason == "runner_error:boom"
    assert result.exit_code == 1
    assert result.started_at == failure_time
    assert result.finished_at == failure_time
    assert result.stdout_log == str((job_dir / "crest.stdout.log").resolve())
    assert result.stderr_log == str((job_dir / "crest.stderr.log").resolve())
    assert result.selected_input_xyz == str(selected_xyz.resolve())
    assert result.manifest_path == str(manifest_path.resolve())
    assert result.resource_request == {"max_cores": 4, "max_memory_gb": 16}
    assert result.resource_actual == {"max_cores": 4, "max_memory_gb": 16}
    assert sleeps == []
    assert artifact_results == [result]
    assert [call["status"] for call in upsert_calls] == ["running", "failed"]
    assert len(mark_failed_calls) == 1
    assert mark_failed_calls[0][0] == (cfg.runtime.allowed_root, entry.queue_id)
    assert mark_failed_calls[0][1]["error"] == "runner_error:boom"
    assert finished_notifications[0]["status"] == "failed"
    assert finished_notifications[0]["reason"] == "runner_error:boom"


def test_process_dequeued_entry_raises_worker_shutdown_requested_before_start(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, molecule_key="shutdown-key")

    deps = _dependencies(
        write_running_state=lambda *args, **kwargs: pytest.fail(
            "running state should not be written"
        ),
        upsert_job_record=lambda *args, **kwargs: pytest.fail("job record should not be updated"),
        notify_job_started=lambda *args, **kwargs: pytest.fail("start notification should not run"),
        start_crest_job=lambda *args, **kwargs: pytest.fail("job should not start"),
    )

    with pytest.raises(WorkerShutdownRequested) as exc_info:
        worker_execution.process_dequeued_entry(
            cfg,
            entry,
            molecule_key_resolver=lambda entry, selected_xyz, job_dir: "shutdown-key",
            dependencies=deps,
            shutdown_requested=lambda: True,
        )

    assert exc_info.value.context.job_dir == job_dir.resolve()
    assert exc_info.value.context.selected_xyz == selected_xyz.resolve()


def test_process_dequeued_entry_raises_worker_shutdown_requested_after_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    entry = _entry(job_dir, selected_xyz, molecule_key="shutdown-key")
    proc = FakeProcess(None)
    running = SimpleNamespace(process=proc)

    terminate_calls: list[FakeProcess] = []
    sleeps: list[int] = []

    monkeypatch.setattr(worker_execution.time, "sleep", lambda seconds: sleeps.append(seconds))

    def terminate(actual_proc: FakeProcess) -> bool:
        terminate_calls.append(actual_proc)
        actual_proc.exit()
        return True

    deps = _dependencies(
        get_cancel_requested=lambda *args, **kwargs: False,
        start_crest_job=lambda cfg, *, job_dir, selected_xyz, execution_snapshot: running,
        terminate_process=terminate,
        finalize_crest_job=lambda *args, **kwargs: pytest.fail("finalize should not run"),
        write_execution_artifacts=lambda *args, **kwargs: pytest.fail(
            "artifacts should not be written"
        ),
        mark_completed=lambda *args, **kwargs: pytest.fail("queue should not be marked completed"),
        mark_cancelled=lambda *args, **kwargs: pytest.fail("queue should not be marked cancelled"),
        mark_failed=lambda *args, **kwargs: pytest.fail("queue should not be marked failed"),
        notify_job_finished=lambda *args, **kwargs: pytest.fail(
            "finish notification should not run"
        ),
    )

    shutdown_checks = iter([False, False, True])
    with pytest.raises(WorkerShutdownRequested):
        worker_execution.process_dequeued_entry(
            cfg,
            entry,
            molecule_key_resolver=lambda entry, selected_xyz, job_dir: "shutdown-key",
            dependencies=deps,
            shutdown_requested=lambda: next(shutdown_checks),
        )

    assert terminate_calls == [proc]
    assert sleeps == []
