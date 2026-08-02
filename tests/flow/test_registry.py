from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.messaging import (
    DiscordBotChannel,
    SendResult,
    render_discord_embed,
)
from orca_auto.flow import registry, worker_state_store
from orca_auto.flow.registry import _notifications as registry_notifications
from orca_auto.flow.registry import store as registry_store
from orca_auto.flow.workflow import journal as workflow_journal
from tests.flow.registry_test_helpers import (
    patch_file_locks as _patch_file_locks,
)
from tests.flow.registry_test_helpers import (
    patch_now_utc_iso as _patch_now_utc_iso,
)


def test_record_from_summary_coerces_counts_and_nested_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:00:00+00:00")

    record = registry_store._record_from_summary(
        {
            "workflow_id": "wf_1",
            "template_name": "reaction_ts_search",
            "status": "planned",
            "source_job_id": "job_1",
            "source_job_type": "xtb_path",
            "reaction_key": "rxn_1",
            "requested_at": "2026-04-19T00:00:00+00:00",
            "workspace_dir": "/tmp/workspace_1",
            "stage_count": "2",
            "stage_status_counts": {"planned": "2", "bad": "nan"},
            "task_status_counts": {"submitted": 1},
            "downstream_reaction_workflow": {"workflow_id": "child_1"},
            "precomplex_handoff": {"reactant_xyz": "/tmp/reactant.xyz"},
            "parent_workflow": {"workflow_id": "parent_1"},
            "final_child_sync_pending": 1,
            "last_restarted_at": "2026-04-19T00:45:00+00:00",
            "restart_summary": {"status": "restarted", "restarted_at": "2026-04-19T00:45:00+00:00"},
        }
    )

    assert record.workflow_id == "wf_1"
    assert record.workflow_file == str(Path("/tmp/workspace_1").resolve() / "workflow.json")
    assert record.stage_count == 2
    assert record.updated_at  # stamped at record-build time
    assert record.stage_status_counts == {"planned": 2}
    assert record.task_status_counts == {"submitted": 1}
    assert record.metadata == {
        "downstream_reaction_workflow": {"workflow_id": "child_1"},
        "precomplex_handoff": {"reactant_xyz": "/tmp/reactant.xyz"},
        "parent_workflow": {"workflow_id": "parent_1"},
        "final_child_sync_pending": True,
        "last_restarted_at": "2026-04-19T00:45:00+00:00",
        "restart_summary": {"status": "restarted", "restarted_at": "2026-04-19T00:45:00+00:00"},
    }


@pytest.mark.parametrize(
    ("event", "expected_lines"),
    [
        (
            {
                "event_type": "workflow_status_changed",
                "workflow_id": "wf_1",
                "template_name": "reaction_ts_search",
                "status": "running",
                "previous_status": "planned",
                "worker_session_id": "session-1",
            },
            [
                "Status changed",
                "Workflow: `wf_1`",
                "Template: `reaction_ts_search`",
                "Status: `planned` → `running`",
            ],
        ),
        (
            {
                "event_type": "workflow_advance_failed",
                "workflow_id": "wf_2",
                "template_name": "reaction_ts_search",
                "reason": "boom",
                "worker_session_id": "session-2",
            },
            [
                "❌ Advance failed",
                "Workflow: `wf_2`",
                "Reason: `boom`",
                "Directory: `-`",
            ],
        ),
        (
            {
                "event_type": "workflow_stage_submitted",
                "workflow_id": "wf_stage",
                "template_name": "reaction_ts_search",
                "stage_id": "xtb_path_search_01",
                "engine": "xtb",
                "task_kind": "path_search",
                "status": "queued",
                "previous_status": "planned",
                "stage_status": "queued",
                "previous_stage_status": "planned",
                "worker_session_id": "session-stage",
            },
            [
                "Stage submitted",
                "Workflow: `wf_stage`",
                "Stage: `xtb_path_search_01`",
                "Task: `xtb/path_search`",
                "Stage status: `planned` → `queued`",
            ],
        ),
        (
            {
                "event_type": "workflow_stage_handoff_ready",
                "workflow_id": "wf_stage",
                "template_name": "reaction_ts_search",
                "stage_id": "xtb_path_search_01",
                "engine": "xtb",
                "task_kind": "path_search",
                "stage_status": "completed",
                "reaction_handoff_status": "ready",
                "previous_reaction_handoff_status": "queued",
                "reason": "xtb_ts_guess_ready",
                "worker_session_id": "session-handoff",
            },
            [
                "✅ Handoff ready",
                "Workflow: `wf_stage`",
                "Stage: `xtb_path_search_01`",
                "Task: `xtb/path_search`",
                "Stage status: `completed`",
                "Reaction handoff: `queued` → `ready`",
                "Reason: `xtb_ts_guess_ready`",
            ],
        ),
        (
            {
                "event_type": "worker_started",
                "reason": "started",
                "worker_session_id": "session-1",
            },
            [
                "Worker started",
                "Workflow root: `/nonexistent/orca-auto-test-root_3`",
                "Reason: `started`",
            ],
        ),
        (
            {
                "event_type": "workflow_phase_finished",
                "workflow_id": "wf_phase",
                "template_name": "reaction_ts_search",
                "status": "mixed",
                "worker_session_id": "session-phase",
                "metadata": {
                    "phase": "xtb",
                    "phase_label": "xTB",
                    "phase_outcome": "mixed",
                    "stage_count": 2,
                    "stage_status_counts": {"completed": 2},
                    "stage_statuses": [
                        {
                            "label": "rxn_01",
                            "stage_id": "xtb_path_search_01",
                            "status": "completed",
                        },
                        {"label": "rxn_02", "stage_id": "xtb_path_search_02", "status": "failed"},
                    ],
                    "reaction_handoff_status_counts": {"ready": 1, "failed": 1},
                    "failure_reasons": ["xtb_ts_guess_missing"],
                },
            },
            [
                "Phase finished",
                "Workflow: `wf_phase`",
                "Phase: `xTB`",
                "Phase outcome: `mixed`",
                "Stage status counts: `completed:2`",
                "Stage statuses: `rxn_01:completed,rxn_02:failed`",
                "Reaction handoff counts: `failed:1,ready:1`",
                "Failure reasons: `xtb_ts_guess_missing`",
            ],
        ),
        (
            {
                "event_type": "custom_event",
                "workflow_id": "wf_4",
                "status": "queued",
                "previous_status": "planned",
                "reason": "started",
                "worker_session_id": "session-1",
            },
            [
                "Workflow event",
                "Event: `custom_event`",
                "Workflow: `wf_4`",
                "Status: `queued`",
            ],
        ),
    ],
)
def test_journal_event_message_formats_supported_event_types(
    event: dict[str, Any],
    expected_lines: list[str],
) -> None:
    embed = render_discord_embed(
        registry_notifications.journal_event_message(event, "/nonexistent/orca-auto-test-root_3")
    )

    # The event identity renders as the embed author; the first expected entry is
    # the embed title (severity glyph included), the rest are "Field: `value`"
    # fragments assembled into a searchable haystack.
    haystack_parts = [embed.get("title", "")]
    author = embed.get("author")
    if author:
        haystack_parts.append(author["name"])
    for item in embed.get("fields", []):
        haystack_parts.append(f"{item['name']}: {item['value']}")
    if embed.get("description"):
        haystack_parts.append(embed["description"])
    haystack = "\n".join(haystack_parts)

    assert embed["title"] == expected_lines[0]
    assert embed["author"] == {"name": "orca_auto"}
    for line in expected_lines[1:]:
        assert line in haystack


def test_notification_configuration_helpers_cover_default_override_and_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES", raising=False)
    monkeypatch.delenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", raising=False)
    monkeypatch.delenv("ORCA_AUTO_CONFIG", raising=False)

    assert registry_notifications.notification_event_types_from_env() == set(
        registry_notifications.DEFAULT_NOTIFICATION_EVENT_TYPES
    )
    assert registry_notifications.journal_notification_enabled("workflow_status_changed") is True
    assert registry_notifications.journal_notification_enabled("workflow_stage_submitted") is False
    assert (
        registry_notifications.journal_notification_enabled("workflow_stage_handoff_ready") is False
    )
    assert registry_notifications.journal_notification_enabled("workflow_phase_finished") is False
    assert registry_notifications.messenger_channel_from_env() is None

    monkeypatch.setenv(
        "ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES",
        "custom_event, workflow_status_changed, workflow_stage_submitted",
    )
    monkeypatch.setenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", "true")
    assert registry_notifications.notification_event_types_from_env() == {
        "custom_event",
        "workflow_stage_submitted",
        "workflow_status_changed",
    }
    assert registry_notifications.journal_notification_enabled("custom_event") is False

    monkeypatch.setenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", "0")
    assert registry_notifications.journal_notification_enabled("custom_event") is True
    assert registry_notifications.messenger_channel_from_env() is None


def test_messenger_channel_from_env_uses_config_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                "messenger:",
                "  provider: discord",
                "  discord:",
                "    bot_token: config-bot-token",
                '    default_channel_id: "123456789012345678"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCA_AUTO_CONFIG", str(config_path))

    channel = registry_notifications.messenger_channel_from_env()

    assert isinstance(channel, DiscordBotChannel)
    assert channel.config.bot_token == "config-bot-token"


def test_maybe_notify_journal_event_sends_message_and_swallows_channel_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_messages: list[Any] = []

    class FakeChannel:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail

        @property
        def enabled(self) -> bool:
            return True

        def send(self, message: Any, *, silent: bool = False) -> SendResult:
            if self.fail:
                raise RuntimeError("channel failed")
            sent_messages.append(message)
            return SendResult(sent=True)

    event = {
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_notify",
        "template_name": "reaction_ts_search",
        "status": "running",
        "previous_status": "planned",
        "worker_session_id": "session-notify",
    }

    monkeypatch.setattr(
        registry_notifications, "journal_notification_enabled", lambda event_type: True
    )
    monkeypatch.setattr(
        registry_notifications, "messenger_channel_from_env", lambda: FakeChannel(fail=False)
    )
    workflow_journal._maybe_notify_journal_event(event, tmp_path)
    workflow_journal._maybe_notify_journal_event(
        {
            "event_type": "workflow_stage_submitted",
            "workflow_id": "wf_notify",
            "template_name": "reaction_ts_search",
            "stage_id": "xtb_path_search_01",
            "engine": "xtb",
            "task_kind": "path_search",
            "metadata": {"engine": "xtb"},
        },
        tmp_path,
    )
    workflow_journal._maybe_notify_journal_event(
        {
            "event_type": "workflow_stage_submitted",
            "workflow_id": "wf_notify",
            "template_name": "reaction_ts_search",
            "stage_id": "orca_ts",
            "engine": "orca",
            "task_kind": "dft",
            "metadata": {"engine": "orca"},
        },
        tmp_path,
    )
    workflow_journal._maybe_notify_journal_event(
        {
            "event_type": "workflow_phase_finished",
            "workflow_id": "wf_notify",
            "template_name": "reaction_ts_search",
            "worker_session_id": "session-notify",
            "metadata": {
                "phase": "xtb",
                "phase_label": "xTB",
                "phase_outcome": "completed",
                "stage_count": 2,
                "stage_status_counts": {"completed": 2},
            },
        },
        tmp_path,
    )

    # The opt-in above was widened to every event type, so every event sends —
    # including the two stage events. Nothing overrides an explicit opt-in.
    assert len(sent_messages) == 4
    status_fields = {
        item["name"]: item["value"]
        for item in render_discord_embed(sent_messages[0]).get("fields", [])
    }
    phase_fields = {
        item["name"]: item["value"]
        for item in render_discord_embed(sent_messages[3]).get("fields", [])
    }
    assert status_fields["Workflow"] == "`wf_notify`"
    assert phase_fields["Phase"] == "`xTB`"

    monkeypatch.setattr(
        registry_notifications, "messenger_channel_from_env", lambda: FakeChannel(fail=True)
    )
    workflow_journal._maybe_notify_journal_event(event, tmp_path)
    assert len(sent_messages) == 4


def test_clear_terminal_workflow_registry_removes_only_terminal_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)

    records = [
        registry.WorkflowRegistryRecord(
            workflow_id="wf-completed",
            template_name="reaction_ts_search",
            status="completed",
            source_job_id="job-1",
            source_job_type="reaction_ts_search",
            reaction_key="rxn-1",
            requested_at="2026-04-19T00:00:00+00:00",
            workspace_dir=str(tmp_path / "wf-completed"),
            workflow_file=str(tmp_path / "wf-completed" / "workflow.json"),
        ),
        registry.WorkflowRegistryRecord(
            workflow_id="wf-running",
            template_name="reaction_ts_search",
            status="running",
            source_job_id="job-2",
            source_job_type="reaction_ts_search",
            reaction_key="rxn-2",
            requested_at="2026-04-19T00:01:00+00:00",
            workspace_dir=str(tmp_path / "wf-running"),
            workflow_file=str(tmp_path / "wf-running" / "workflow.json"),
        ),
        registry.WorkflowRegistryRecord(
            workflow_id="wf-cancelled",
            template_name="reaction_ts_search",
            status="cancelled",
            source_job_id="job-3",
            source_job_type="reaction_ts_search",
            reaction_key="rxn-3",
            requested_at="2026-04-19T00:02:00+00:00",
            workspace_dir=str(tmp_path / "wf-cancelled"),
            workflow_file=str(tmp_path / "wf-cancelled" / "workflow.json"),
        ),
    ]
    registry_store._save_records(tmp_path, records)

    assert registry.clear_terminal_workflow_registry(tmp_path) == 2
    remaining = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [record.workflow_id for record in remaining] == ["wf-running"]
    assert registry.clear_terminal_workflow_registry(tmp_path) == 0


def test_clear_terminal_workflow_registry_prevents_reindex_resurrection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:20:00+00:00")

    completed_workspace = tmp_path / "wf-completed"
    running_workspace = tmp_path / "wf-running"
    completed_workspace.mkdir()
    running_workspace.mkdir()
    completed_payload = {
        "workflow_id": "wf-completed",
        "template_name": "reaction_ts_search",
        "status": "completed",
        "source_job_id": "job-1",
        "source_job_type": "reaction_ts_search",
        "reaction_key": "rxn-1",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (completed_workspace / "workflow.json").write_text(
        json.dumps(completed_payload), encoding="utf-8"
    )
    (running_workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": "wf-running",
                "template_name": "reaction_ts_search",
                "status": "running",
                "source_job_id": "job-2",
                "source_job_type": "reaction_ts_search",
                "reaction_key": "rxn-2",
                "requested_at": "2026-04-19T00:01:00+00:00",
                "stages": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
    assert [
        record.workflow_id
        for record in registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    ] == ["wf-running"]

    reindexed = registry.reindex_workflow_registry(tmp_path)
    assert [record.workflow_id for record in reindexed] == ["wf-running"]

    completed_payload["status"] = "running"
    (completed_workspace / "workflow.json").write_text(
        json.dumps(completed_payload), encoding="utf-8"
    )
    assert {record.workflow_id for record in registry.reindex_workflow_registry(tmp_path)} == {
        "wf-completed",
        "wf-running",
    }

    completed_payload["status"] = "completed"
    (completed_workspace / "workflow.json").write_text(
        json.dumps(completed_payload), encoding="utf-8"
    )
    assert {record.workflow_id for record in registry.reindex_workflow_registry(tmp_path)} == {
        "wf-completed",
        "wf-running",
    }


def test_terminal_workflow_with_pending_si_publication_is_not_clearable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-pending-si"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf-pending-si",
        "template_name": "conformer_screening",
        "status": "completed",
        "source_job_id": "job-pending-si",
        "source_job_type": "conformer_screening",
        "reaction_key": "input",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_pending": True, "si_publish_generation": "generation-1"},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry.sync_workflow_registry(tmp_path, workspace, payload)

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [
        (record.workflow_id, record.metadata.get("si_publish_pending")) for record in records
    ] == [("wf-pending-si", True)]
    assert not registry_store._cleared_path(tmp_path).exists()

    reindexed = registry.reindex_workflow_registry(tmp_path)
    assert [
        (record.workflow_id, record.metadata.get("si_publish_pending")) for record in reindexed
    ] == [("wf-pending-si", True)]


@pytest.mark.parametrize("pending_flag", ["si_publish_pending", "final_child_sync_pending"])
def test_clear_consults_authoritative_pending_when_registry_sync_lagged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pending_flag: str,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-authoritative-pending"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf-authoritative-pending",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {pending_flag: True},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    stale_record = registry.WorkflowRegistryRecord(
        workflow_id="wf-authoritative-pending",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [stale_record])
    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    assert registry.get_workflow_registry_record(tmp_path, "wf-authoritative-pending") is not None


def test_clear_keeps_stale_terminal_row_when_authoritative_restart_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-restart-sync-lag"
    workspace.mkdir()
    active_payload = {
        "workflow_id": "wf-restart-sync-lag",
        "template_name": "conformer_screening",
        "status": "planned",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(active_payload), encoding="utf-8")
    stale_record = registry.WorkflowRegistryRecord(
        workflow_id="wf-restart-sync-lag",
        template_name="conformer_screening",
        status="failed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [stale_record])

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    assert registry.get_workflow_registry_record(tmp_path, "wf-restart-sync-lag") is not None


def test_clear_keeps_row_while_pending_checkpoint_holds_workflow_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-checkpoint-race"
    workspace.mkdir()
    payload: dict[str, Any] = {
        "workflow_id": "wf-checkpoint-race",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry.sync_workflow_registry(tmp_path, workspace, payload)

    with registry_store.acquire_workflow_lock(workspace):
        payload["metadata"]["si_publish_pending"] = True
        (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
        # Simulate a crash/pause before registry sync. Clear must fail closed
        # instead of observing the old cached row and racing the checkpoint.
        assert registry.clear_terminal_workflow_registry(tmp_path) == 0

    record = registry.get_workflow_registry_record(tmp_path, "wf-checkpoint-race")
    assert record is not None


def test_clear_never_locks_registry_workspace_outside_direct_root_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "nested" / "wf-outside"
    workspace.mkdir(parents=True)
    (workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": "wf-outside",
                "template_name": "conformer_screening",
                "status": "completed",
                "stages": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf-outside",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    assert not (workspace / "workflow.lock").exists()
    assert registry.get_workflow_registry_record(tmp_path, "wf-outside") is not None


def test_clear_missing_payload_does_not_create_workflow_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-missing-payload"
    workspace.mkdir()
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf-missing-payload",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])

    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
    assert not (workspace / "workflow.lock").exists()


@pytest.mark.parametrize(
    "flag", ["si_publish_pending", "si_publish_blocked", "final_child_sync_pending"]
)
def test_stale_registry_publication_guard_is_not_clearable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / f"wf-{flag}"
    workspace.mkdir()
    (workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": f"wf-{flag}",
                "template_name": "conformer_screening",
                "status": "completed",
                "stages": [],
                "metadata": {flag: False},
            }
        ),
        encoding="utf-8",
    )
    record = registry.WorkflowRegistryRecord(
        workflow_id=f"wf-{flag}",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
        metadata={flag: True},
    )
    registry_store._save_records(tmp_path, [record])
    assert registry.clear_terminal_workflow_registry(tmp_path) == 0


def test_sync_quarantined_identity_uses_workspace_key_without_mutating_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
            }
        },
    }

    record = registry.sync_workflow_registry(tmp_path, workspace, payload)

    assert payload["workflow_id"] == "wf_tampered"
    assert record.workflow_id == "wf_expected"
    assert record.metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


def test_sync_prequarantine_identity_mismatch_stays_visible_by_workspace_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }

    record = registry.sync_workflow_registry(tmp_path, workspace, payload)

    # The durable payload is left alone and the row stays addressable by its
    # trusted workspace identity. A row that has not been quarantined yet
    # carries no cached identity marker; the authoritative recheck happens
    # under the lock at clear time.
    assert payload["workflow_id"] == "wf_tampered"
    assert record.workflow_id == "wf_expected"
    assert "identity_reconciliation_persisted_workflow_id" not in record.metadata
    assert "identity_quarantined" not in record.metadata


def test_identity_quarantine_resurrects_a_previously_cleared_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    valid_payload = {
        "workflow_id": "wf_expected",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(valid_payload), encoding="utf-8")
    registry.sync_workflow_registry(tmp_path, workspace, valid_payload)
    assert registry.clear_terminal_workflow_registry(tmp_path) == 1

    quarantined_payload = {
        **valid_payload,
        "workflow_id": "wf_tampered",
        "status": "failed",
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
            }
        },
    }
    (workspace / "workflow.json").write_text(json.dumps(quarantined_payload), encoding="utf-8")

    registry.sync_workflow_registry(tmp_path, workspace, quarantined_payload)

    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"
    assert registry_store._load_cleared_markers(tmp_path) == []


def test_upsert_replaces_all_rows_for_the_same_trusted_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()

    def record(workflow_id: str, status: str) -> registry.WorkflowRegistryRecord:
        return registry.WorkflowRegistryRecord(
            workflow_id=workflow_id,
            template_name="conformer_screening",
            status=status,
            source_job_id="",
            source_job_type="",
            reaction_key="",
            requested_at="2026-04-19T00:00:00+00:00",
            workspace_dir=str(workspace),
            workflow_file=str(workspace / "workflow.json"),
        )

    registry_store._save_records(
        tmp_path,
        [record("wf_expected", "running"), record("wf_tampered", "failed")],
    )
    replacement = record("wf_expected", "failed")

    registry.upsert_workflow_registry_record(tmp_path, replacement)

    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == [replacement]


def test_upsert_rejects_unvalidated_direct_workspace_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    expected = registry.WorkflowRegistryRecord(
        workflow_id="wf_expected",
        template_name="conformer_screening",
        status="running",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [expected])
    tampered = replace(expected, workflow_id="wf_tampered", status="cancelled")

    with pytest.raises(ValueError, match="does not match workspace name"):
        registry.upsert_workflow_registry_record(tmp_path, tampered)

    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == [expected]


def test_sync_skips_cleared_terminal_workflow_until_it_becomes_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:20:00+00:00")

    workspace = tmp_path / "wf-completed"
    workspace.mkdir()
    terminal_payload = {
        "workflow_id": "wf-completed",
        "template_name": "reaction_ts_search",
        "status": "completed",
        "source_job_id": "job-1",
        "source_job_type": "reaction_ts_search",
        "reaction_key": "rxn-1",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(terminal_payload), encoding="utf-8")

    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
    registry.sync_workflow_registry(tmp_path, workspace, terminal_payload)
    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == []

    active_payload = dict(terminal_payload)
    active_payload["status"] = "running"
    registry.sync_workflow_registry(tmp_path, workspace, active_payload)
    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf-completed", "running")
    ]


@pytest.mark.parametrize("markers_payload", ["{invalid", json.dumps({"bad": True})])
def test_workflow_cleared_markers_corrupt_payload_blocks_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    markers_payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf_completed",
        template_name="reaction_ts_search",
        status="completed",
        source_job_id="job_completed",
        source_job_type="xtb_path",
        reaction_key="rxn_completed",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf_completed"),
        workflow_file=str(tmp_path / "wf_completed" / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])
    registry_text = registry_store._registry_path(tmp_path).read_text(encoding="utf-8")
    registry_store._cleared_path(tmp_path).write_text(markers_payload, encoding="utf-8")

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.clear_terminal_workflow_registry(tmp_path)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.upsert_workflow_registry_record(tmp_path, record)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.reindex_workflow_registry(tmp_path)
    assert registry_store._registry_path(tmp_path).read_text(encoding="utf-8") == registry_text
    assert registry_store._cleared_path(tmp_path).read_text(encoding="utf-8") == markers_payload


def test_list_workflow_registry_does_not_reindex_valid_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    registry_store._save_records(tmp_path, [])

    def fake_reindex_workflow_registry(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        raise AssertionError(f"unexpected reindex for {root}")

    monkeypatch.setattr(registry_store, "reindex_workflow_registry", fake_reindex_workflow_registry)

    assert registry.list_workflow_registry(tmp_path) == []


def test_list_workflow_registry_repairs_published_creation_marker_before_cached_return(
    tmp_path: Path,
) -> None:
    registry_store._save_records(tmp_path, [])
    workspace = tmp_path / "wf_crash_repair"
    workspace.mkdir()
    (workspace / "workflow.json").write_text("{}", encoding="utf-8")
    marker = workspace / ".orca_auto_workflow_creation.json"
    marker.write_text("{}", encoding="utf-8")
    record = registry.WorkflowRegistryRecord(
        workflow_id=workspace.name,
        template_name="conformer_screening",
        status="planned",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        requested_at="2026-07-13T00:00:00+00:00",
        workspace_dir=str(workspace.resolve()),
        workflow_file=str((workspace / "workflow.json").resolve()),
    )
    calls: list[Path] = []

    def repair(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        calls.append(Path(root).resolve())
        return [record]

    assert registry.list_workflow_registry(tmp_path, reindex_fn=repair) == [record]
    assert calls == [tmp_path.resolve()]
    assert not marker.exists()


def test_list_workflow_registry_keeps_marker_when_reindex_skips_workspace(tmp_path: Path) -> None:
    registry_store._save_records(tmp_path, [])
    workspace = tmp_path / "wf_corrupt_repair"
    workspace.mkdir()
    (workspace / "workflow.json").write_text("not-json", encoding="utf-8")
    marker = workspace / ".orca_auto_workflow_creation.json"
    marker.write_text("{}", encoding="utf-8")

    assert registry.list_workflow_registry(tmp_path, reindex_fn=lambda _root: []) == []
    assert marker.exists()


def test_list_workflow_registry_ignores_unpublished_creation_marker(tmp_path: Path) -> None:
    registry_store._save_records(tmp_path, [])
    workspace = tmp_path / "wf_incomplete_reservation"
    workspace.mkdir()
    marker = workspace / ".orca_auto_workflow_creation.json"
    marker.write_text("{}", encoding="utf-8")

    def unexpected_reindex(_root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        raise AssertionError("unpublished reservations must not trigger reindex")

    assert registry.list_workflow_registry(tmp_path, reindex_fn=unexpected_reindex) == []
    assert marker.exists()


def test_list_workflow_registry_missing_without_reindex_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)

    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == []
    assert not registry_store._registry_path(tmp_path).exists()


def test_clear_terminal_workflow_registry_empty_status_filter_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf-completed",
        template_name="reaction_ts_search",
        status="completed",
        source_job_id="job-1",
        source_job_type="xtb_path",
        reaction_key="rxn-1",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf-completed"),
        workflow_file=str(tmp_path / "wf-completed" / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])

    assert registry.clear_terminal_workflow_registry(tmp_path, statuses={"", "  "}) == 0
    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == [record]
    assert not registry_store._cleared_path(tmp_path).exists()


def test_registry_read_os_error_raises_corrupt_error() -> None:
    class BrokenPath:
        def exists(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            raise OSError("permission denied")

        def __str__(self) -> str:
            return "/broken/workflow_registry.json"

    with pytest.raises(registry.WorkflowRegistryCorruptError, match="cannot be read"):
        registry_store._read_existing_json(
            BrokenPath(),  # type: ignore[arg-type]
            description="Workflow registry file",
            missing_default=[],
        )


@pytest.mark.parametrize("payload", ["{invalid", json.dumps({"workflow_id": "wf-bad"})])
def test_list_workflow_registry_rejects_corrupt_existing_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    registry_store._registry_path(tmp_path).write_text(payload, encoding="utf-8")
    reindex_calls: list[Path] = []

    def fake_reindex_workflow_registry(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        reindex_calls.append(Path(root).resolve())
        return []

    monkeypatch.setattr(registry_store, "reindex_workflow_registry", fake_reindex_workflow_registry)

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.list_workflow_registry(tmp_path)
    assert reindex_calls == []
    assert registry_store._registry_path(tmp_path).read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("registry_payload", ["{invalid", json.dumps({"workflow_id": "wf-bad"})])
def test_workflow_registry_writes_reject_corrupt_existing_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry_payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    registry_store._registry_path(tmp_path).write_text(registry_payload, encoding="utf-8")
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf_safe",
        template_name="reaction_ts_search",
        status="planned",
        source_job_id="job_safe",
        source_job_type="xtb_path",
        reaction_key="rxn_safe",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf_safe"),
        workflow_file=str(tmp_path / "wf_safe" / "workflow.json"),
    )

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.upsert_workflow_registry_record(tmp_path, record)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.reindex_workflow_registry(tmp_path)
    assert registry_store._registry_path(tmp_path).read_text(encoding="utf-8") == registry_payload


def test_append_workflow_journal_event_writes_jsonl_and_returns_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    notifications: list[dict[str, Any]] = []
    token_values = iter(["wf_evt_1", "wf_evt_2", "wf_evt_3"])
    time_values = iter(
        [
            "2026-04-19T01:00:00+00:00",
            "2026-04-19T01:05:00+00:00",
            "2026-04-19T01:10:00+00:00",
        ]
    )

    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(workflow_journal, "timestamped_token", lambda prefix: next(token_values))
    _patch_now_utc_iso(monkeypatch, lambda: next(time_values))
    monkeypatch.setattr(
        workflow_journal,
        "_maybe_notify_journal_event",
        lambda event, workflow_root: notifications.append(
            {"event": dict(event), "workflow_root": str(Path(workflow_root).resolve())}
        ),
    )

    first = registry.append_workflow_journal_event(
        tmp_path,
        event_type="workflow_status_changed",
        workflow_id="wf_1",
        template_name="reaction_ts_search",
        status="running",
        previous_status="planned",
        reason="advanced",
        worker_session_id="session-1",
        metadata={"attempt": 1},
    )
    second = registry.append_workflow_journal_event(
        tmp_path,
        event_type="workflow_stage_submitted",
        workflow_id="wf_2",
        template_name="reaction_ts_search",
        stage_id="xtb_path_search_01",
        engine="xtb",
        task_kind="path_search",
        stage_status="queued",
        previous_stage_status="planned",
        worker_session_id="session-stage",
    )
    third = registry.append_workflow_journal_event(
        tmp_path,
        event_type="workflow_stage_handoff_ready",
        workflow_id="wf_2",
        template_name="reaction_ts_search",
        stage_id="xtb_path_search_01",
        engine="xtb",
        task_kind="path_search",
        stage_status="completed",
        reaction_handoff_status="ready",
        previous_reaction_handoff_status="queued",
        reason="xtb_ts_guess_ready",
    )

    journal_path = registry.workflow_journal_path(tmp_path)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    second_raw = json.loads(lines[1])
    third_raw = json.loads(lines[2])

    assert first["event_id"] == "wf_evt_1"
    assert first["occurred_at"] == "2026-04-19T01:00:00+00:00"
    assert first["metadata"] == {"attempt": 1}
    assert second["event_id"] == "wf_evt_2"
    assert third["event_id"] == "wf_evt_3"
    assert len(lines) == 3
    assert json.loads(lines[0])["workflow_id"] == "wf_1"
    assert second_raw["workflow_id"] == "wf_2"
    assert second_raw["stage_id"] == "xtb_path_search_01"
    assert second_raw["engine"] == "xtb"
    assert second_raw["task_kind"] == "path_search"
    assert second_raw["previous_stage_status"] == "planned"
    assert second_raw["stage_status"] == "queued"
    assert third_raw["reaction_handoff_status"] == "ready"
    assert third_raw["previous_reaction_handoff_status"] == "queued"
    assert notifications[0]["workflow_root"] == str(tmp_path.resolve())
    assert notifications[1]["event"]["stage_status"] == "queued"
    assert notifications[2]["event"]["reason"] == "xtb_ts_guess_ready"


def test_list_workflow_journal_uses_append_commit_order_and_applies_bounded_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    journal_path = registry.workflow_journal_path(tmp_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_1",
                        "occurred_at": "2026-04-19T00:01:00+00:00",
                        "event_type": "a",
                    }
                ),
                "not-json",
                "",
                json.dumps(
                    {
                        "event_id": "evt_3",
                        "occurred_at": "2026-04-19T00:03:00+00:00",
                        "event_type": "c",
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_2",
                        "occurred_at": "2026-04-19T00:02:00+00:00",
                        "event_type": "b",
                    }
                ),
                "{truncated",
            ]
        ),
        encoding="utf-8",
    )
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(
        workflow_journal,
        "read_confined_text",
        lambda *args, **kwargs: pytest.fail("bounded journal reads must not load the full file"),
    )

    result = registry.list_workflow_journal(tmp_path, limit=2)

    assert [item["event_id"] for item in result] == ["evt_2", "evt_3"]


def test_write_and_load_workflow_worker_state_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(worker_state_store.os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_state_store.socket, "gethostname", lambda: "host-1")
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T02:00:00+00:00")

    written = registry.write_workflow_worker_state(
        tmp_path,
        worker_session_id="session-42",
        status="running",
        workflow_root_path=tmp_path / "custom_root",
        last_cycle_started_at="2026-04-19T01:00:00+00:00",
        interval_seconds=30.0,
        submit_ready=True,
        metadata={"cycle": 1},
    )
    loaded = registry.load_workflow_worker_state(tmp_path)

    assert written == {
        "worker_session_id": "session-42",
        "status": "running",
        "workflow_root": str((tmp_path / "custom_root").resolve()),
        "pid": 4242,
        "hostname": "host-1",
        "last_heartbeat_at": "2026-04-19T02:00:00+00:00",
        "last_cycle_started_at": "2026-04-19T01:00:00+00:00",
        "last_cycle_finished_at": "",
        "lease_expires_at": "",
        "interval_seconds": 30.0,
        "submit_ready": True,
        "metadata": {"cycle": 1},
    }
    assert loaded == written


def test_workflow_worker_state_coalesces_unchanged_heartbeat_until_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(worker_state_store.os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_state_store.socket, "gethostname", lambda: "host-1")
    real_atomic_write_json = worker_state_store.atomic_write_json
    writes: list[dict[str, Any]] = []

    def tracked_atomic_write_json(path: Path, payload: dict[str, Any], **kwargs: Any) -> None:
        writes.append(dict(payload))
        real_atomic_write_json(path, payload, **kwargs)

    monkeypatch.setattr(worker_state_store, "atomic_write_json", tracked_atomic_write_json)
    common: dict[str, Any] = {
        "worker_session_id": "session-42",
        "status": "running",
        "workflow_root_path": tmp_path,
        "interval_seconds": 30.0,
        "submit_ready": True,
        "metadata": {"discovered_count": 1, "failed_count": 0},
        "minimum_heartbeat_interval_seconds": 60.0,
    }

    first = registry.write_workflow_worker_state(
        tmp_path,
        **common,
        last_heartbeat_at="2026-04-19T02:00:00+00:00",
        lease_expires_at="2026-04-19T02:01:00+00:00",
    )
    coalesced = registry.write_workflow_worker_state(
        tmp_path,
        **common,
        last_cycle_finished_at="2026-04-19T02:00:30+00:00",
        last_heartbeat_at="2026-04-19T02:00:30+00:00",
        lease_expires_at="2026-04-19T02:01:30+00:00",
    )
    due = registry.write_workflow_worker_state(
        tmp_path,
        **common,
        last_cycle_finished_at="2026-04-19T02:01:00+00:00",
        last_heartbeat_at="2026-04-19T02:01:00+00:00",
        lease_expires_at="2026-04-19T02:02:00+00:00",
    )

    assert len(writes) == 2
    assert coalesced == first
    assert due["last_heartbeat_at"] == "2026-04-19T02:01:00+00:00"


def test_workflow_worker_state_writes_semantic_change_before_heartbeat_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(worker_state_store.os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_state_store.socket, "gethostname", lambda: "host-1")
    registry.write_workflow_worker_state(
        tmp_path,
        worker_session_id="session-42",
        status="running",
        last_heartbeat_at="2026-04-19T02:00:00+00:00",
        metadata={"failed_count": 0},
    )

    changed = registry.write_workflow_worker_state(
        tmp_path,
        worker_session_id="session-42",
        status="running",
        last_heartbeat_at="2026-04-19T02:00:10+00:00",
        metadata={"failed_count": 1},
        minimum_heartbeat_interval_seconds=60.0,
    )

    assert changed["last_heartbeat_at"] == "2026-04-19T02:00:10+00:00"
    assert registry.load_workflow_worker_state(tmp_path)["metadata"] == {"failed_count": 1}


@pytest.mark.parametrize("state_payload", ["{invalid", json.dumps(["bad"])])
def test_workflow_worker_state_corrupt_payload_raises_and_write_does_not_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    state_path = registry.workflow_worker_state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state_payload, encoding="utf-8")

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.load_workflow_worker_state(tmp_path)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.write_workflow_worker_state(
            tmp_path,
            worker_session_id="session-safe",
            status="running",
        )
    assert state_path.read_text(encoding="utf-8") == state_payload


def test_upsert_list_get_and_resolve_workflow_registry_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    record_older = registry.WorkflowRegistryRecord(
        workflow_id="wf_a",
        template_name="reaction_ts_search",
        status="planned",
        source_job_id="job_a",
        source_job_type="xtb_path",
        reaction_key="rxn_a",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf_a"),
        workflow_file=str(tmp_path / "wf_a" / "workflow.json"),
    )
    record_newer = registry.WorkflowRegistryRecord(
        workflow_id="wf_b",
        template_name="conformer_screening",
        status="running",
        source_job_id="job_b",
        source_job_type="raw_xyz",
        reaction_key="rxn_b",
        requested_at="2026-04-19T00:05:00+00:00",
        workspace_dir=str(tmp_path / "wf_b"),
        workflow_file=str(tmp_path / "wf_b" / "workflow.json"),
    )
    record_updated = registry.WorkflowRegistryRecord(
        workflow_id="wf_a",
        template_name="reaction_ts_search",
        status="completed",
        source_job_id="job_a",
        source_job_type="xtb_path",
        reaction_key="rxn_a",
        requested_at="2026-04-19T00:10:00+00:00",
        workspace_dir=str(tmp_path / "wf_a"),
        workflow_file=str(tmp_path / "wf_a" / "workflow.json"),
    )

    registry.upsert_workflow_registry_record(tmp_path, record_older)
    registry.upsert_workflow_registry_record(tmp_path, record_newer)
    registry.upsert_workflow_registry_record(tmp_path, record_updated)

    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)

    assert [(item.workflow_id, item.status) for item in records] == [
        ("wf_a", "completed"),
        ("wf_b", "running"),
    ]
    assert registry.get_workflow_registry_record(tmp_path, "wf_b") == record_newer
    assert registry.resolve_workflow_registry_record(tmp_path, "wf_a") == record_updated
    assert (
        registry.resolve_workflow_registry_record(tmp_path, str(tmp_path / "wf_b")) == record_newer
    )
    assert (
        registry.resolve_workflow_registry_record(
            tmp_path, str(tmp_path / "wf_a" / "workflow.json")
        )
        == record_updated
    )
    assert registry.resolve_workflow_registry_record(tmp_path, "") is None


def test_list_workflow_registry_reindexes_when_missing_and_reindex_skips_bad_workspaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    original_reindex = registry.reindex_workflow_registry

    list_result = [
        registry.WorkflowRegistryRecord(
            workflow_id="wf_reindexed",
            template_name="reaction_ts_search",
            status="planned",
            source_job_id="job_reindexed",
            source_job_type="xtb_path",
            reaction_key="rxn_reindexed",
            requested_at="2026-04-19T00:00:00+00:00",
            workspace_dir=str(tmp_path / "wf_reindexed"),
            workflow_file=str(tmp_path / "wf_reindexed" / "workflow.json"),
        )
    ]
    reindex_calls: list[Path] = []

    def fake_reindex_workflow_registry(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        reindex_calls.append(Path(root).resolve())
        return list_result

    monkeypatch.setattr(registry_store, "reindex_workflow_registry", fake_reindex_workflow_registry)
    assert registry.list_workflow_registry(tmp_path) == list_result
    assert reindex_calls == [tmp_path.resolve()]
    monkeypatch.setattr(registry_store, "reindex_workflow_registry", original_reindex)

    good_workspace = tmp_path / "wf_good"
    bad_workspace = tmp_path / "wf_bad"
    summaries = {
        good_workspace: {
            "workflow_id": "wf_good",
            "template_name": "conformer_screening",
            "status": "planned",
            "source_job_id": "job_good",
            "source_job_type": "raw_xyz",
            "reaction_key": "rxn_good",
            "requested_at": "2026-04-19T00:15:00+00:00",
            "workspace_dir": str(good_workspace),
            "stage_count": 1,
            "stage_status_counts": {"planned": 1},
            "task_status_counts": {"planned": 1},
        }
    }

    def fake_iter_workflow_workspaces(root: Path) -> list[Path]:
        return [good_workspace, bad_workspace]

    def fake_load_workflow_payload(workspace_dir: Path) -> dict[str, Any]:
        if workspace_dir == bad_workspace:
            raise FileNotFoundError("missing workflow")
        return {"workflow_id": "wf_good"}

    def fake_workflow_summary(workspace_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        return summaries[workspace_dir]

    monkeypatch.setattr(registry_store, "iter_workflow_workspaces", fake_iter_workflow_workspaces)
    monkeypatch.setattr(registry_store, "load_workflow_payload", fake_load_workflow_payload)
    monkeypatch.setattr(registry_store, "workflow_summary", fake_workflow_summary)
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:20:00+00:00")

    records = registry.reindex_workflow_registry(tmp_path)

    assert len(records) == 1
    assert records[0].workflow_id == "wf_good"
    assert (
        registry.sync_workflow_registry(
            tmp_path, good_workspace, {"workflow_id": "wf_good"}
        ).workflow_id
        == "wf_good"
    )
