from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.messaging import SendResult, render_discord_embed
from orca_auto.flow import orchestration, registry, runtime
from orca_auto.flow.registry import _notifications as registry_notifications
from orca_auto.flow.registry import store as registry_store
from tests.flow.orchestration_services import orchestration_services


def _write_xyz_ensemble(path: Path, comments: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for comment in comments:
        lines.extend(
            [
                "2",
                comment,
                "H 0 0 0",
                "H 0 0 0.74",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cancel_materialized_workflow_mixes_local_remote_and_failed_cancellations(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_01",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_completed",
                "status": "completed",
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "stage_id": "stage_local",
                "status": "planned",
                "task": {"engine": "crest", "status": "planned"},
            },
            {
                "stage_id": "stage_crest_remote",
                "status": "queued",
                "metadata": {"queue_id": "q_crest"},
                "task": {"engine": "crest", "status": "queued"},
            },
            {
                "stage_id": "stage_xtb_missing_config",
                "status": "running",
                "metadata": {"queue_id": "q_xtb"},
                "task": {"engine": "xtb", "status": "running"},
            },
            {
                "stage_id": "stage_orca_remote",
                "status": "submitted",
                "metadata": {"queue_id": "q_orca"},
                "task": {"engine": "orca", "status": "submitted"},
            },
        ],
    }

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: tmp_path / "wf_cancel_01",
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "crest_cancel_target": lambda **kwargs: {
                "status": "cancel_requested",
                "queue_id": kwargs["target"],
            },
            "orca_cancel_target": lambda **kwargs: {
                "status": "cancelled",
                "queue_id": kwargs["target"],
            },
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_01",
        workflow_root=tmp_path,
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert result["status"] == "cancel_requested"
    assert result["cancelled"] == [
        {"stage_id": "stage_local", "mode": "local"},
        {"stage_id": "stage_crest_remote", "status": "cancel_requested"},
        {"stage_id": "stage_orca_remote", "status": "cancelled"},
    ]
    assert result["failed"] == [
        {"stage_id": "stage_xtb_missing_config", "reason": "missing_engine_config"},
    ]
    assert payload["stages"][1]["status"] == "cancelled"
    assert payload["stages"][1]["task"]["status"] == "cancelled"
    assert payload["stages"][2]["task"]["cancel_result"]["status"] == "cancel_requested"
    assert payload["stages"][3]["task"]["cancel_result"]["reason"] == "missing_engine_config"
    assert payload["stages"][4]["task"]["cancel_result"]["status"] == "cancelled"


def test_cancel_materialized_workflow_reports_cancelled_when_no_remote_request_pending(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_02",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_local",
                "status": "queued",
                "task": {"engine": "crest", "status": "queued"},
            }
        ],
    }
    events: list[dict[str, Any]] = []

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: tmp_path / "wf_cancel_02",
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
            "append_workflow_journal_event": lambda workflow_root, **kwargs: events.append(
                {"workflow_root": workflow_root, **kwargs}
            ),
        }
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_02",
        workflow_root=tmp_path,
        services=deps,
    )

    assert result["status"] == "cancelled"
    assert result["cancelled"] == [{"stage_id": "stage_local", "mode": "local"}]
    assert result["failed"] == []
    assert len(events) == 1
    assert events[0]["workflow_root"] == tmp_path.resolve()
    assert events[0]["event_id"].startswith("wf_evt_")
    assert events[0]["occurred_at"]
    assert {
        key: value for key, value in events[0].items() if key not in {"event_id", "occurred_at"}
    } == {
        "workflow_root": tmp_path.resolve(),
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_cancel_02",
        "template_name": "",
        "previous_status": "running",
        "status": "cancelled",
        "reason": "cancel_requested",
        "worker_session_id": "workflow_cancel",
    }

    repeated = orchestration.cancel_materialized_workflow(
        target="wf_cancel_02",
        workflow_root=tmp_path,
        services=deps,
    )

    assert repeated["status"] == "cancelled"
    assert len(events) == 1


def test_cancel_materialized_workflow_journals_and_notifies_terminal_transition_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_notify_once",
        "template_name": "conformer_screening",
        "status": "running",
        "stages": [],
    }
    messages: list[Any] = []

    class RecordingChannel:
        def send(self, message: Any, *, silent: bool = False) -> SendResult:
            messages.append(message)
            return SendResult(sent=True)

    monkeypatch.delenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", raising=False)
    monkeypatch.delenv("ORCA_AUTO_FLOW_NOTIFY_EVENTS", raising=False)
    monkeypatch.setattr(
        registry_notifications,
        "messenger_channel_from_env",
        lambda: RecordingChannel(),
    )
    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_cancel_notify_once"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
            "append_workflow_journal_event": registry.append_workflow_journal_event,
        }
    )

    first = orchestration.cancel_materialized_workflow(
        target="wf_cancel_notify_once",
        workflow_root=tmp_path,
        services=deps,
    )
    second = orchestration.cancel_materialized_workflow(
        target="wf_cancel_notify_once",
        workflow_root=tmp_path,
        services=deps,
    )

    assert first["status"] == second["status"] == "cancelled"
    [event] = registry.list_workflow_journal(tmp_path)
    assert event["event_type"] == "workflow_status_changed"
    assert event["previous_status"] == "running"
    assert event["status"] == "cancelled"
    assert len(messages) == 1
    embed = render_discord_embed(messages[0])
    assert embed["title"] == "Status changed"
    fields = {item["name"]: item["value"] for item in embed["fields"]}
    assert fields["Status"] == "`running` → `cancelled`"


def test_cancel_materialized_workflow_retries_event_after_registry_sync_failure(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_registry_retry",
        "status": "running",
        "stages": [],
    }
    sync_attempts = 0
    events: list[dict[str, Any]] = []

    def sync_once_then_succeed(*_args: Any) -> None:
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts == 1:
            raise OSError("registry durability barrier failed")

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_cancel_registry_retry"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": sync_once_then_succeed,
            "append_workflow_journal_event": lambda workflow_root, **kwargs: events.append(
                {"workflow_root": workflow_root, **kwargs}
            ),
        }
    )

    with pytest.raises(OSError, match="registry durability barrier failed"):
        orchestration.cancel_materialized_workflow(
            target="wf_cancel_registry_retry",
            workflow_root=tmp_path,
            services=deps,
        )

    assert payload["status"] == "cancelled"
    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_registry_retry",
        workflow_root=tmp_path,
        services=deps,
    )

    assert result["status"] == "cancelled"
    assert sync_attempts == 3
    assert len(events) == 1
    assert events[0]["previous_status"] == "running"
    assert events[0]["status"] == "cancelled"


def test_cancel_materialized_workflow_preserves_pending_child_status_on_retry(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_requested_retry",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_xtb_pending",
                "status": "running",
                "metadata": {"queue_id": "q_xtb_pending"},
                "task": {"engine": "xtb", "status": "running"},
            }
        ],
    }
    cancel_calls = 0
    sync_attempts = 0
    events: list[dict[str, Any]] = []

    def request_cancel(**_kwargs: Any) -> dict[str, Any]:
        nonlocal cancel_calls
        cancel_calls += 1
        return {"status": "cancel_requested", "queue_id": "q_xtb_pending"}

    def sync_once_then_succeed(*_args: Any) -> None:
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts == 1:
            raise OSError("registry durability barrier failed")

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_cancel_requested_retry"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": sync_once_then_succeed,
            "xtb_cancel_target": request_cancel,
            "append_workflow_journal_event": lambda workflow_root, **kwargs: events.append(
                {"workflow_root": workflow_root, **kwargs}
            ),
        }
    )

    with pytest.raises(OSError, match="registry durability barrier failed"):
        orchestration.cancel_materialized_workflow(
            target="wf_cancel_requested_retry",
            workflow_root=tmp_path,
            xtb_config="/tmp/xtb.yaml",
            services=deps,
        )

    assert payload["status"] == "cancel_requested"
    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_requested_retry",
        workflow_root=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        services=deps,
    )

    assert result["status"] == "cancel_requested"
    assert payload["stages"][0]["status"] == "cancel_requested"
    assert payload["stages"][0]["task"]["status"] == "cancel_requested"
    assert cancel_calls == 1
    assert [(event["previous_status"], event["status"]) for event in events] == [
        ("running", "cancel_requested")
    ]


def test_cancel_materialized_workflow_retries_uncertain_journal_append_once(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_journal_retry",
        "status": "running",
        "stages": [],
    }
    durable_events: dict[str, dict[str, Any]] = {}
    append_attempts = 0

    def append_once_then_report_uncertain(_workflow_root: Path, **event: Any) -> dict[str, Any]:
        nonlocal append_attempts
        append_attempts += 1
        event_id = str(event["event_id"])
        existing = durable_events.setdefault(event_id, dict(event))
        if append_attempts == 1:
            raise OSError("journal append outcome uncertain")
        return existing

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_cancel_journal_retry"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
            "append_workflow_journal_event": append_once_then_report_uncertain,
        }
    )

    with pytest.raises(OSError, match="journal append outcome uncertain"):
        orchestration.cancel_materialized_workflow(
            target="wf_cancel_journal_retry",
            workflow_root=tmp_path,
            services=deps,
        )

    pending = payload["metadata"]["cancellation_status_transitions"]
    assert len(pending) == 1
    event_id = pending[0]["event_id"]
    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_journal_retry",
        workflow_root=tmp_path,
        services=deps,
    )

    assert result["status"] == "cancelled"
    assert append_attempts == 2
    assert list(durable_events) == [event_id]
    assert payload["metadata"]["cancellation_status_transitions"] == []


def test_cancel_materialized_workflow_recovers_journal_after_directory_fsync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orca_auto.flow.registry import journal as workflow_journal

    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_directory_fsync_retry",
        "status": "running",
        "stages": [],
    }
    directory_fsync_attempts = 0
    messages: list[Any] = []

    class RecordingChannel:
        def send(self, message: Any, *, silent: bool = False) -> SendResult:
            messages.append(message)
            return SendResult(sent=True)

    def fail_first_directory_fsync(_path: Path) -> None:
        nonlocal directory_fsync_attempts
        directory_fsync_attempts += 1
        if directory_fsync_attempts == 1:
            raise OSError("journal directory durability barrier failed")

    monkeypatch.delenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", raising=False)
    monkeypatch.delenv("ORCA_AUTO_FLOW_NOTIFY_EVENTS", raising=False)
    monkeypatch.setattr(
        registry_notifications,
        "messenger_channel_from_env",
        lambda: RecordingChannel(),
    )
    monkeypatch.setattr(workflow_journal, "fsync_directory", fail_first_directory_fsync)
    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_cancel_directory_fsync_retry"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
            "append_workflow_journal_event": registry.append_workflow_journal_event,
        }
    )

    with pytest.raises(OSError, match="journal directory durability barrier failed"):
        orchestration.cancel_materialized_workflow(
            target="wf_cancel_directory_fsync_retry",
            workflow_root=tmp_path,
            services=deps,
        )

    assert messages == []
    assert len(payload["metadata"]["cancellation_status_transitions"]) == 1

    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_directory_fsync_retry",
        workflow_root=tmp_path,
        services=deps,
    )

    assert result["status"] == "cancelled"
    assert directory_fsync_attempts == 2
    assert len(registry.list_workflow_journal(tmp_path)) == 1
    assert messages == []
    assert payload["metadata"]["cancellation_status_transitions"] == []


def test_cancel_materialized_workflow_retries_after_child_cancel_before_payload_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orca_auto.core.queue import QueueStatus, enqueue, list_queue
    from orca_auto.flow.submitters import xtb as xtb_submitter

    queue_root = tmp_path / "queue"
    child = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="xtb-child",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={"job_dir": str(tmp_path / "xtb-child"), "job_type": "opt"},
    )
    durable_payload: dict[str, Any] = {
        "workflow_id": "wf_child_cancel_payload_retry",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_xtb",
                "status": "queued",
                "metadata": {"queue_id": child.queue_id},
                "task": {"engine": "xtb", "status": "queued"},
            }
        ],
    }
    writes = 0

    def load_payload(_workspace_dir: Path) -> dict[str, Any]:
        return copy.deepcopy(durable_payload)

    def write_once_then_persist(_workspace_dir: Path, payload: dict[str, Any]) -> None:
        nonlocal writes, durable_payload
        writes += 1
        if writes == 1:
            raise OSError("parent payload durability barrier failed")
        durable_payload = copy.deepcopy(payload)

    monkeypatch.setattr(xtb_submitter, "load_queue_config", lambda _path: object())
    monkeypatch.setattr(
        xtb_submitter,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_root, current) for current in list_queue(queue_root)],
    )
    monkeypatch.setattr(
        xtb_submitter,
        "_before_pending_cancel",
        lambda _entry, *, config_path: None,
    )
    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_child_cancel_payload_retry"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": load_payload,
            "write_workflow_payload": write_once_then_persist,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, payload: None,
            "xtb_cancel_target": xtb_submitter.cancel_target,
        }
    )

    with pytest.raises(OSError, match="parent payload durability barrier failed"):
        orchestration.cancel_materialized_workflow(
            target="wf_child_cancel_payload_retry",
            workflow_root=tmp_path,
            xtb_config="/tmp/xtb.yaml",
            services=deps,
        )

    [cancelled_child] = list_queue(queue_root)
    assert cancelled_child.status == QueueStatus.CANCELLED
    assert durable_payload["status"] == "running"

    result = orchestration.cancel_materialized_workflow(
        target="wf_child_cancel_payload_retry",
        workflow_root=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        services=deps,
    )

    assert result["status"] == "cancelled"
    assert result["failed"] == []
    assert result["cancelled"] == [{"stage_id": "stage_xtb", "status": "cancelled"}]
    assert durable_payload["status"] == "cancelled"
    assert durable_payload["stages"][0]["status"] == "cancelled"
    assert durable_payload["stages"][0]["task"]["status"] == "cancelled"


def test_cancel_materialized_workflow_reports_cancel_failed_when_stage_cancellation_fails(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_failed_cancel",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_orca_remote",
                "status": "submitted",
                "metadata": {"queue_id": "q_orca"},
                "task": {"engine": "orca", "status": "submitted"},
            },
        ],
    }

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_failed_cancel"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "orca_cancel_target": lambda **kwargs: {
                "status": "failed",
                "reason": "cancel_command_timeout",
            },
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_failed_cancel",
        workflow_root=tmp_path,
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert result["status"] == "cancel_failed"
    assert result["cancelled"] == []
    assert result["failed"] == [
        {"stage_id": "stage_orca_remote", "reason": "cancel_command_timeout"}
    ]


def test_cancel_materialized_workflow_reports_busy_lock_timeout(
    tmp_path: Path,
) -> None:
    def fake_acquire_workflow_lock(workspace_dir, timeout_seconds=5.0):
        raise TimeoutError("Timed out acquiring lock")

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: tmp_path / "workspace",
            "acquire_workflow_lock": fake_acquire_workflow_lock,
        }
    )

    with pytest.raises(
        ValueError, match="Workflow is busy and could not be locked for cancellation within 5s"
    ):
        orchestration.cancel_materialized_workflow(
            target="wf_busy",
            workflow_root=tmp_path,
            services=deps,
        )


def test_cancel_quarantines_identity_mismatch_without_registry_duplicate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "running",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_blocked": True},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry_store._save_records(
        tmp_path,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf_expected",
                template_name="conformer_screening",
                status="running",
                source_job_id="",
                source_job_type="",
                reaction_key="",
                requested_at="2026-07-12T00:00:00+00:00",
                workspace_dir=str(workspace),
                workflow_file=str(workspace / "workflow.json"),
            )
        ],
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_expected",
        workflow_root=tmp_path,
    )
    cycle = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path,
        submit_ready=False,
        worker_session_id="post-quarantine-cancel",
        lease_seconds=0,
    )

    assert result["workflow_id"] == "wf_expected"
    assert result["status"] == "failed"
    assert (cycle["advanced_count"], cycle["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


def test_cancel_clears_stale_identity_error_after_workspace_recovery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "wf_restored"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_restored",
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
                "reason": "old mismatch",
            }
        },
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")

    result = orchestration.cancel_materialized_workflow(
        target="wf_restored",
        workflow_root=tmp_path,
    )

    assert result["status"] == "cancelled"
    persisted = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert "workflow_error" not in persisted["metadata"]
    record = registry.get_workflow_registry_record(tmp_path, "wf_restored")
    assert record is not None
    assert "identity_quarantined" not in record.metadata
