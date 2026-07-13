from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.config import CommonResourceConfig, CommonRuntimeConfig
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.engine_runner import engine_runtime_identity
from orca_auto.core.indexing import get_job_location, list_job_locations
from orca_auto.core.queue import dequeue_next, enqueue, list_queue, mark_cancelled, request_cancel
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.internal_engine import own_engine_accept_entry
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.flow.engines.crest import queue_runtime as queue_cmd
from orca_auto.flow.engines.crest.runner import CrestRunResult
from orca_auto.flow.engines.crest.state import (
    REPORT_JSON_FILE_NAME,
    REPORT_MD_FILE_NAME,
    STATE_FILE_NAME,
    load_report_json,
    load_state,
    write_report_json,
    write_state,
)
from tests.engine_artifact_helpers import artifact_payload
from tests.engine_artifact_helpers import (
    artifacts as _artifacts,
)
from tests.engine_artifact_helpers import (
    engine_payload as _engine_payload,
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
from tests.engine_process_helpers import process_one_crest_for_test
from tests.execution_snapshot_helpers import stage_execution_snapshot


def _find_entry_by_target(entries: list[Any], target: str) -> Any | None:
    for entry in entries:
        if entry.queue_id == target or entry.task_id == target:
            return entry
    return None


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


class FakeChildProcess(FakeProcess):
    def __init__(self, pid: int, *poll_values: int | None) -> None:
        super().__init__(*poll_values)
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._poll_values = [0]

    def kill(self) -> None:
        self.kill_calls += 1
        self._poll_values = [-9]


@pytest.fixture
def queue_env(tmp_path: Path) -> SimpleNamespace:
    allowed_root = tmp_path / "allowed_root"
    allowed_root.mkdir()
    cfg = AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(allowed_root),
            max_concurrent=2,
            admission_root=str(tmp_path / "admission_root"),
            admission_limit=1,
        ),
        resources=CommonResourceConfig(max_cores_per_task=4, max_memory_gb_per_task=16),
    )
    return SimpleNamespace(
        cfg=cfg,
        allowed_root=allowed_root,
        tmp_path=tmp_path,
    )


def _enqueue_job(
    env: SimpleNamespace,
    *,
    task_id: str,
    mode: str = "standard",
    molecule_key: str | None = None,
) -> SimpleNamespace:
    job_dir = env.allowed_root / "jobs" / task_id
    job_dir.mkdir(parents=True)
    selected_xyz = job_dir / "selected_input.xyz"
    selected_xyz.write_text("1\nselected\nH 0.0 0.0 0.0\n", encoding="utf-8")
    resolved_molecule_key = molecule_key or selected_xyz.stem
    runtime_identity = engine_runtime_identity(job_dir)
    selected_snapshot, execution_snapshot = stage_execution_snapshot(
        job_dir,
        selected_xyz,
        engine="crest",
        manifest={"mode": mode, "_orca_auto_runtime_identity": runtime_identity},
        resource_request={"max_cores": 4, "max_memory_gb": 16},
        identity={
            "mode": mode,
            "molecule_key": resolved_molecule_key,
            "runtime_identity": runtime_identity,
        },
    )
    metadata = {
        "job_dir": str(job_dir),
        "selected_input_xyz": str(selected_snapshot),
        "mode": mode,
        "resource_request": {"max_cores": 4, "max_memory_gb": 16},
        "execution_snapshot": execution_snapshot,
        "molecule_key": resolved_molecule_key,
    }
    entry = enqueue(
        env.allowed_root,
        app_name="orca_auto_crest",
        task_id=task_id,
        task_kind="crest_conformer_search",
        engine="crest",
        metadata=metadata,
    )
    return SimpleNamespace(entry=entry, job_dir=job_dir, selected_xyz=selected_snapshot)


def _make_result(
    job_dir: Path,
    selected_xyz: Path,
    *,
    status: str,
    reason: str,
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
        retained_path = job_dir / name
        retained_path.write_text("1\nretained\nH 0.0 0.0 0.0\n", encoding="utf-8")
        retained_paths.append(str(retained_path.resolve()))

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


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_worker_adopts_terminal_artifacts_before_orphan_requeue(queue_env: SimpleNamespace) -> None:
    job = _enqueue_job(queue_env, task_id="terminal-adoption", molecule_key="mol")
    running = dequeue_next(queue_env.allowed_root)
    assert running is not None
    current_claim = replace(running, started_at="2026-04-19T00:00:00+00:00")
    result = _make_result(
        job.job_dir,
        job.selected_xyz,
        status="completed",
        reason="completed",
        retained_names=("crest_conformers.xyz",),
    )
    queue_cmd._write_execution_artifacts(running, result)

    assert queue_cmd._adopt_terminal_artifacts(
        queue_env.cfg,
        queue_env.allowed_root,
        current_claim,
    )

    [terminal] = list_queue(queue_env.allowed_root)
    assert terminal.status == QueueStatus.COMPLETED
    indexed = get_job_location(queue_env.allowed_root, "terminal-adoption")
    assert indexed is not None
    assert indexed.status == "completed"


def test_terminal_adoption_finalizes_racing_cancel_consistently(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="crest-cancel-adoption", molecule_key="mol")
    running = dequeue_next(queue_env.allowed_root)
    assert running is not None
    current_claim = replace(running, started_at="2026-04-19T00:00:00+00:00")
    result = _make_result(
        job.job_dir,
        job.selected_xyz,
        status="completed",
        reason="completed",
        retained_names=("crest_conformers.xyz",),
    )
    queue_cmd._write_execution_artifacts(running, result)
    assert (
        request_cancel(
            queue_env.allowed_root,
            running.queue_id,
            expected_entry=running,
        )
        is not None
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(
        queue_env.cfg,
        queue_env.allowed_root,
        current_claim,
    )

    [cancelled] = list_queue(queue_env.allowed_root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert cancelled.cancel_requested is False
    assert [row["status"] for row in upserts] == ["cancelled"]
    persisted_state = load_state(job.job_dir)
    persisted_report = load_report_json(job.job_dir)
    assert persisted_state is not None
    assert persisted_report is not None
    assert persisted_state["status"]["state"] == "cancelled"
    assert persisted_report["status"]["state"] == "cancelled"
    assert "Status: `cancelled`" in (job.job_dir / REPORT_MD_FILE_NAME).read_text(encoding="utf-8")


def test_worker_rejects_report_older_than_exact_running_state(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="running-state-stale-report", molecule_key="mol")
    running = dequeue_next(queue_env.allowed_root)
    assert running is not None
    common_fields: dict[str, Any] = {
        "engine": "crest",
        "job_id": running.task_id,
        "queue_id": running.queue_id,
        "app_name": running.app_name,
        "task_id": running.task_id,
        "primary_path": str(job.selected_xyz),
        "engine_payload": {"mode": "standard", "molecule_key": "mol"},
    }
    write_state(
        job.job_dir,
        artifact_payload(
            **common_fields,
            job_dir=str(job.job_dir),
            status="running",
            reason="current_attempt",
        ),
    )
    write_report_json(
        job.job_dir,
        artifact_payload(
            **common_fields,
            status="completed",
            reason="stale_attempt",
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(
        queue_env.cfg,
        queue_env.allowed_root,
        running,
    )
    [unchanged] = list_queue(queue_env.allowed_root)
    assert unchanged.status == QueueStatus.RUNNING
    assert upserts == []


def test_worker_rejects_report_when_exact_state_status_is_malformed(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="malformed-current-state", molecule_key="mol")
    running = dequeue_next(queue_env.allowed_root)
    assert running is not None
    common_fields: dict[str, Any] = {
        "engine": "crest",
        "job_id": running.task_id,
        "queue_id": running.queue_id,
        "app_name": running.app_name,
        "task_id": running.task_id,
        "primary_path": str(job.selected_xyz),
        "engine_payload": {"mode": "standard", "molecule_key": "mol"},
    }
    malformed_state = artifact_payload(
        **common_fields,
        job_dir=str(job.job_dir),
        status="completed",
        reason="current_attempt",
    )
    malformed_state["status"] = None
    write_state(job.job_dir, malformed_state)
    write_report_json(
        job.job_dir,
        artifact_payload(
            **common_fields,
            status="completed",
            reason="stale_attempt",
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(
        queue_env.cfg,
        queue_env.allowed_root,
        running,
    )
    [unchanged] = list_queue(queue_env.allowed_root)
    assert unchanged.status == QueueStatus.RUNNING
    assert upserts == []


def test_terminal_adoption_rebuilds_foreign_state_from_exact_report(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="crest-current-report", molecule_key="mol")
    running = dequeue_next(queue_env.allowed_root)
    assert running is not None
    write_state(
        job.job_dir,
        artifact_payload(
            engine="crest",
            job_id="foreign-job",
            queue_id="foreign-queue",
            app_name=running.app_name,
            task_id="foreign-job",
            job_dir=str(job.job_dir),
            status="failed",
            primary_path=str(job.selected_xyz),
            updated_at="2999-01-01T00:00:00+00:00",
            engine_payload={"mode": "standard", "molecule_key": "foreign"},
        ),
    )
    write_report_json(
        job.job_dir,
        artifact_payload(
            engine="crest",
            job_id=running.task_id,
            queue_id=running.queue_id,
            app_name=running.app_name,
            task_id=running.task_id,
            generation=queue_entry_generation_token(running),
            status="completed",
            reason="current_completed",
            primary_path=str(job.selected_xyz),
            resource_request={"max_cores": 4, "max_memory_gb": 16},
            updated_at="2999-01-01T00:00:01+00:00",
            engine_payload={"mode": "standard", "molecule_key": "mol"},
        ),
    )
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda *_args, **_kwargs: None)

    assert queue_cmd._adopt_terminal_artifacts(
        queue_env.cfg,
        queue_env.allowed_root,
        running,
    )
    state = load_state(job.job_dir)
    report = load_report_json(job.job_dir)
    assert state is not None and report is not None
    assert state["job"]["id"] == running.task_id
    assert state["job"]["queue_id"] == running.queue_id
    assert report["job"]["id"] == running.task_id
    assert state["status"]["state"] == report["status"]["state"] == "completed"
    assert "Status: `completed`" in (job.job_dir / REPORT_MD_FILE_NAME).read_text(encoding="utf-8")


def test_worker_repairs_crest_publication_before_claim(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = queue_env.allowed_root / "jobs" / "repair-publication"
    job_dir.mkdir(parents=True)
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = enqueue(
        queue_env.allowed_root,
        app_name="orca_auto_crest",
        task_id="crest-repair-job",
        task_kind="crest_conformer_search",
        engine="crest",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "mode": "standard",
            "molecule_key": "repair",
            "resource_request": {},
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token="crest-repair-token",
                owner_pid=0,
            ),
        },
    )
    recorded: list[str] = []

    def record_queued(_cfg: object, _submission: object, current: Any) -> bool:
        recorded.append(str(current.task_id))
        return True

    monkeypatch.setattr(
        queue_cmd,
        "_record_queued_submission",
        record_queued,
    )

    assert queue_cmd._repair_crest_queue_publications(SimpleNamespace(cfg=queue_env.cfg))

    [repaired] = list_queue(queue_env.allowed_root)
    assert recorded == [entry.task_id]
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    claimed = dequeue_next(
        queue_env.allowed_root,
        accept_entry_fn=own_engine_accept_entry("crest"),
    )
    assert claimed is not None and claimed.queue_id == entry.queue_id


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {
        "started": [],
        "finished": [],
        "released": [],
    }

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda cfg: "slot-1")

    def fake_notify_started(cfg: AppConfig, **kwargs: object) -> bool:
        calls["started"].append(kwargs)
        return True

    def fake_notify_finished(cfg: AppConfig, **kwargs: object) -> bool:
        calls["finished"].append(kwargs)
        return True

    def fake_release_slot(root: str, token: str) -> None:
        calls["released"].append((root, token))

    monkeypatch.setattr(queue_cmd, "notify_job_started", fake_notify_started)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", fake_notify_finished)
    monkeypatch.setattr(queue_cmd, "release_slot", fake_release_slot)
    return calls


def test_process_one_completed_updates_queue_artifacts_index_without_organizing(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = _enqueue_job(queue_env, task_id="job-complete", mode="nci")
    manifest_path = job.job_dir / "crest_job.yaml"
    manifest_path.write_text("mode: nci\n", encoding="utf-8")
    completed_result = _make_result(
        job.job_dir,
        job.selected_xyz,
        status="completed",
        reason="ok",
        mode="nci",
        retained_names=("crest_conformers.xyz", "crest_best.xyz"),
    )
    calls = _patch_common(monkeypatch)

    monkeypatch.setattr(
        queue_cmd,
        "start_crest_job",
        lambda cfg, *, job_dir, selected_xyz, execution_snapshot: SimpleNamespace(
            process=FakeProcess(0)
        ),
    )
    monkeypatch.setattr(queue_cmd, "finalize_crest_job", lambda running: completed_result)

    outcome = process_one_crest_for_test(queue_cmd, queue_env.cfg)

    assert outcome == "processed"
    entry = list_queue(queue_env.allowed_root)[0]
    assert entry.status == QueueStatus.COMPLETED
    assert entry.error == ""
    assert entry.metadata["retained_conformer_count"] == 2
    assert entry.metadata["mode"] == "nci"

    state_payload = _read_json(job.job_dir / STATE_FILE_NAME)
    assert _status(state_payload)["state"] == "completed"
    assert _status(state_payload)["reason"] == "ok"
    assert _engine_payload(state_payload)["molecule_key"] == "selected_input"
    assert _engine_payload(state_payload)["retained_conformer_count"] == 2

    report_payload = _read_json(job.job_dir / REPORT_JSON_FILE_NAME)
    assert _status(report_payload)["state"] == "completed"
    assert _status(report_payload)["reason"] == "ok"
    assert _engine_payload(report_payload)["mode"] == "nci"
    assert _artifacts(report_payload)["manifest_path"] == str(manifest_path.resolve())
    assert _engine_payload(report_payload)["retained_conformer_paths"] == list(
        completed_result.retained_conformer_paths
    )

    report_md = (job.job_dir / REPORT_MD_FILE_NAME).read_text(encoding="utf-8")
    assert "Queue ID" in report_md
    assert "crest_conformers.xyz" in report_md

    records = list_job_locations(queue_env.allowed_root)
    assert len(records) == 1
    record = records[0]
    assert record.job_id == "job-complete"
    assert record.status == "completed"
    assert record.job_type == "crest_nci_conformer_search"
    assert record.original_run_dir == str(job.job_dir.resolve())
    assert record.latest_known_path == str(job.job_dir.resolve())
    assert record.molecule_key == "selected_input"

    assert len(calls["started"]) == 1
    assert calls["started"][0]["queue_id"] == job.entry.queue_id
    assert calls["started"][0]["job_dir"] == job.job_dir.resolve()
    assert len(calls["finished"]) == 1
    assert calls["finished"][0]["status"] == "completed"
    assert calls["finished"][0]["retained_conformer_count"] == 2
    assert calls["released"] == [(queue_env.cfg.runtime.resolved_admission_root, "slot-1")]

    stdout = capsys.readouterr().out
    assert "organized_output_dir:" not in stdout
    assert "status: completed" in stdout


def test_process_one_runner_failure_marks_failed_and_writes_failure_artifacts(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = _enqueue_job(queue_env, task_id="job-failed", molecule_key="fixed-key")
    manifest_path = job.job_dir / "crest_job.yaml"
    manifest_path.write_text("mode: standard\n", encoding="utf-8")
    calls = _patch_common(monkeypatch)

    def boom(
        cfg: AppConfig,
        *,
        job_dir: Path,
        selected_xyz: Path,
        execution_snapshot: dict[str, Any],
    ) -> SimpleNamespace:
        raise RuntimeError("boom")

    monkeypatch.setattr(queue_cmd, "start_crest_job", boom)

    outcome = process_one_crest_for_test(queue_cmd, queue_env.cfg)

    assert outcome == "processed"
    entry = list_queue(queue_env.allowed_root)[0]
    assert entry.status == QueueStatus.FAILED
    assert entry.error == "runner_error:boom"
    assert entry.metadata["retained_conformer_count"] == 0
    assert entry.metadata["mode"] == "standard"

    state_payload = _read_json(job.job_dir / STATE_FILE_NAME)
    assert _status(state_payload)["state"] == "failed"
    assert _status(state_payload)["reason"] == "runner_error:boom"
    assert _engine_payload(state_payload)["molecule_key"] == "fixed-key"
    assert _resources(state_payload)["request"] == {"max_cores": 4, "max_memory_gb": 16}

    report_payload = _read_json(job.job_dir / REPORT_JSON_FILE_NAME)
    assert _status(report_payload)["state"] == "failed"
    assert _status(report_payload)["reason"] == "runner_error:boom"
    assert _engine_payload(report_payload)["command"] == []
    assert _artifacts(report_payload)["manifest_path"] == str(manifest_path.resolve())
    assert _artifacts(report_payload)["stdout_log"] == str(
        (job.job_dir / "crest.stdout.log").resolve()
    )
    assert _artifacts(report_payload)["stderr_log"] == str(
        (job.job_dir / "crest.stderr.log").resolve()
    )

    record = get_job_location(queue_env.allowed_root, "job-failed")
    assert record is not None
    assert record.status == "failed"
    assert record.original_run_dir == str(job.job_dir.resolve())
    assert record.latest_known_path == str(job.job_dir.resolve())
    assert record.molecule_key == "fixed-key"

    assert len(calls["started"]) == 1
    assert len(calls["finished"]) == 1
    assert calls["finished"][0]["status"] == "failed"
    assert calls["finished"][0]["reason"] == "runner_error:boom"
    assert calls["released"] == [(queue_env.cfg.runtime.resolved_admission_root, "slot-1")]

    stdout = capsys.readouterr().out
    assert "status: failed" in stdout


def test_process_one_cancel_requested_terminates_and_marks_cancelled(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = _enqueue_job(queue_env, task_id="job-cancelled")
    cancelled_result = _make_result(
        job.job_dir,
        job.selected_xyz,
        status="cancelled",
        reason="cancel_requested",
        exit_code=143,
    )
    calls = _patch_common(monkeypatch)
    process = FakeProcess(None)
    terminate_calls: list[FakeProcess] = []
    finalize_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(
        queue_cmd,
        "start_crest_job",
        lambda cfg, *, job_dir, selected_xyz, execution_snapshot: SimpleNamespace(process=process),
    )

    def fake_get_cancel_requested(
        root: str,
        queue_id: str,
        **_kwargs: object,
    ) -> bool:
        request_cancel(root, queue_id)
        return True

    def fake_finalize(
        running: SimpleNamespace,
        forced_status: str | None = None,
        forced_reason: str | None = None,
    ) -> CrestRunResult:
        finalize_calls.append((forced_status, forced_reason))
        return cancelled_result

    monkeypatch.setattr(queue_cmd, "get_cancel_requested", fake_get_cancel_requested)

    def terminate(proc: FakeProcess) -> bool:
        terminate_calls.append(proc)
        proc.exit()
        return True

    monkeypatch.setattr(queue_cmd, "_terminate_process", terminate)
    monkeypatch.setattr(queue_cmd, "finalize_crest_job", fake_finalize)

    outcome = process_one_crest_for_test(queue_cmd, queue_env.cfg)

    assert outcome == "processed"
    entry = list_queue(queue_env.allowed_root)[0]
    assert entry.status == QueueStatus.CANCELLED
    assert entry.cancel_requested is False
    assert entry.error == "cancel_requested"
    assert entry.metadata["retained_conformer_count"] == 0
    assert entry.metadata["mode"] == "standard"

    state_payload = _read_json(job.job_dir / STATE_FILE_NAME)
    assert _status(state_payload)["state"] == "cancelled"
    assert _status(state_payload)["reason"] == "cancel_requested"

    report_payload = _read_json(job.job_dir / REPORT_JSON_FILE_NAME)
    assert _status(report_payload)["state"] == "cancelled"
    assert _status(report_payload)["reason"] == "cancel_requested"
    assert _status(report_payload)["exit_code"] == 143

    record = get_job_location(queue_env.allowed_root, "job-cancelled")
    assert record is not None
    assert record.status == "cancelled"
    assert record.latest_known_path == str(job.job_dir.resolve())

    assert terminate_calls == [process]
    assert finalize_calls == [("cancelled", "cancel_requested")]
    assert len(calls["started"]) == 1
    assert len(calls["finished"]) == 1
    assert calls["finished"][0]["status"] == "cancelled"
    assert calls["released"] == [(queue_env.cfg.runtime.resolved_admission_root, "slot-1")]

    stdout = capsys.readouterr().out
    assert "status: cancelled" in stdout


def test_queue_worker_fill_slots_starts_multiple_child_processes(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(queue_env.allowed_root),
            max_concurrent=2,
            admission_root=str(queue_env.tmp_path / "admission_root"),
            admission_limit=2,
        ),
        resources=queue_env.cfg.resources,
    )
    job_one = _enqueue_job(queue_env, task_id="pool-job-001")
    job_two = _enqueue_job(queue_env, task_id="pool-job-002")
    tokens = iter(["slot-1", "slot-2"])
    started_commands: list[list[str]] = []
    activated_slots: list[tuple[Path, str, dict[str, object]]] = []
    child_processes = [
        FakeChildProcess(5101, None),
        FakeChildProcess(5102, None),
    ]

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda cfg_obj: next(tokens))

    def fake_popen(command: list[str], **kwargs: object) -> FakeChildProcess:
        started_commands.append(command)
        return child_processes.pop(0)

    def fake_activate_reserved_slot(
        root: Path | str, token: str, **kwargs: object
    ) -> SimpleNamespace:
        activated_slots.append((Path(root), token, dict(kwargs)))
        return SimpleNamespace(token=token)

    monkeypatch.setattr(queue_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        queue_cmd,
        "activate_reserved_slot",
        fake_activate_reserved_slot,
    )

    worker = queue_cmd.QueueWorker(cfg, "/tmp/orca_auto.yaml", max_concurrent=2)
    worker._fill_slots()

    assert sorted(worker._running) == sorted([job_one.entry.queue_id, job_two.entry.queue_id])
    assert len(started_commands) == 2
    assert all("orca_auto.core.engines.worker_child" in command for command in started_commands)
    assert all(command[command.index("--engine") + 1] == "crest" for command in started_commands)
    assert all("--auto-organize" not in command for command in started_commands)
    assert {command[command.index("--queue-id") + 1] for command in started_commands} == {
        job_one.entry.queue_id,
        job_two.entry.queue_id,
    }
    assert [slot[1] for slot in activated_slots] == ["slot-1", "slot-2"]
    assert [slot[2]["owner_pid"] for slot in activated_slots] == [5101, 5102]

    entries = sorted(list_queue(queue_env.allowed_root), key=lambda item: item.task_id)
    assert [entry.status for entry in entries] == [QueueStatus.RUNNING, QueueStatus.RUNNING]


def test_queue_worker_shutdown_requeues_running_children(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="pool-shutdown-001")
    queue_root, entry = queue_cmd.dequeue_next_entry(queue_env.cfg) or pytest.fail(
        "expected dequeued entry"
    )
    released: list[tuple[Path, str]] = []
    child = FakeChildProcess(6201, None)

    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((Path(root), token))
    )

    worker = queue_cmd.QueueWorker(queue_env.cfg, "/tmp/orca_auto.yaml", max_concurrent=2)
    worker._shutdown_requested = True
    worker._running[entry.queue_id] = queue_cmd._RunningJob(
        queue_root=queue_root,
        entry=entry,
        process=cast(Any, child),
        admission_token="slot-1",
    )

    worker._shutdown_all()

    updated = _find_entry_by_target(list_queue(queue_env.allowed_root), job.entry.queue_id)
    assert updated is not None
    assert updated.status == QueueStatus.PENDING
    assert updated.cancel_requested is False
    assert child.terminate_calls == 1
    assert released == [(worker.admission_root, "slot-1")]
    state = load_state(job.job_dir)
    assert state is not None
    assert _status(state)["state"] == "queued"
    assert _status(state)["reason"] == "worker_shutdown"
    assert _recovery(state)["pending"] is True


def test_queue_worker_reconcile_orphaned_running_requeues_entry_without_live_slot(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan_job = _enqueue_job(queue_env, task_id="orphan-running")
    live_job = _enqueue_job(queue_env, task_id="live-running")
    orphan_root, orphan_entry = queue_cmd.dequeue_next_entry(queue_env.cfg) or pytest.fail(
        "expected orphan entry"
    )
    live_root, live_entry = queue_cmd.dequeue_next_entry(queue_env.cfg) or pytest.fail(
        "expected live entry"
    )

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda root: 0)
    monkeypatch.setattr(
        queue_cmd, "list_slots", lambda root: [SimpleNamespace(queue_id=live_entry.queue_id)]
    )

    worker = queue_cmd.QueueWorker(queue_env.cfg, "/tmp/orca_auto.yaml", max_concurrent=2)
    worker._reconcile_orphaned_running()

    orphan_updated = _find_entry_by_target(
        list_queue(queue_env.allowed_root), orphan_job.entry.queue_id
    )
    live_updated = _find_entry_by_target(
        list_queue(queue_env.allowed_root), live_job.entry.queue_id
    )
    assert orphan_root == live_root
    assert orphan_updated is not None
    assert live_updated is not None
    assert orphan_updated.status == QueueStatus.PENDING
    assert live_updated.status == QueueStatus.RUNNING
    state = load_state(orphan_job.job_dir)
    assert state is not None
    assert _status(state)["state"] == "queued"
    assert _status(state)["reason"] == "crashed_recovery"
    assert _recovery(state)["pending"] is True


def test_queue_worker_reconcile_orphaned_cancel_requested_marks_cancelled(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="orphan-cancelled")
    queue_root, entry = queue_cmd.dequeue_next_entry(queue_env.cfg) or pytest.fail(
        "expected dequeued entry"
    )
    request_cancel(queue_root, entry.queue_id)

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda root: 0)
    monkeypatch.setattr(queue_cmd, "list_slots", lambda root: [])

    worker = queue_cmd.QueueWorker(queue_env.cfg, "/tmp/orca_auto.yaml", max_concurrent=2)
    worker._reconcile_orphaned_running()

    updated = _find_entry_by_target(list_queue(queue_env.allowed_root), job.entry.queue_id)
    assert updated is not None
    assert updated.status == QueueStatus.CANCELLED
    assert updated.error == "cancel_requested"


def test_terminal_reconcile_repairs_partial_cancelled_artifacts(
    queue_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _enqueue_job(queue_env, task_id="crest-partial-terminal", molecule_key="mol")
    running = dequeue_next(queue_env.allowed_root)
    assert running is not None
    cancelled = mark_cancelled(
        queue_env.allowed_root,
        running.queue_id,
        error="cancel_requested",
        expected_entry=running,
    )
    assert cancelled is not None
    common_fields: dict[str, Any] = {
        "engine": "crest",
        "job_id": running.task_id,
        "queue_id": running.queue_id,
        "app_name": running.app_name,
        "task_id": running.task_id,
        "generation": queue_entry_generation_token(cancelled),
        "primary_path": str(job.selected_xyz),
        "updated_at": "2999-01-01T00:00:00+00:00",
        "engine_payload": {"mode": "standard", "molecule_key": "mol"},
    }
    write_state(
        job.job_dir,
        artifact_payload(
            **common_fields,
            job_dir=str(job.job_dir),
            status="cancelled",
            reason="cancel_requested",
        ),
    )
    write_report_json(
        job.job_dir,
        artifact_payload(**common_fields, status="completed", reason="completed"),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(
        queue_cmd,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_env.allowed_root, cancelled)],
    )
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    queue_cmd._sync_terminal_running_entries(SimpleNamespace(cfg=queue_env.cfg))

    state = load_state(job.job_dir)
    report = load_report_json(job.job_dir)
    assert state is not None and report is not None
    assert state["status"]["state"] == report["status"]["state"] == "cancelled"
    assert upserts[-1]["status"] == "cancelled"
    assert "Status: `cancelled`" in (job.job_dir / REPORT_MD_FILE_NAME).read_text(encoding="utf-8")
