"""Workflow registry notification rendering and configuration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.messaging import DiscordBotChannel, render_discord_embed
from orca_auto.flow.registry import _notifications as notifications
from orca_auto.flow.workflow._phases import WORKFLOW_PHASE_FINISHED_EVENT

# A root that can never exist keeps the Directory resolution
# deterministic for the fixture events (rendered as '-').
_ROOT = "/nonexistent/orca-auto-test-wfroot"


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf1",
        "template_name": "tmpl",
        "status": "running",
        "previous_status": "queued",
        "reason": "because",
        "worker_session_id": "sess<1>",
        "metadata": {},
    }
    base.update(overrides)
    return base


def _embed(event: dict[str, Any]) -> dict[str, Any]:
    return render_discord_embed(notifications.journal_event_message(event, _ROOT))


def _fields(embed: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["value"] for item in embed.get("fields", [])}


def test_status_changed_render() -> None:
    # Identity renders as the embed author (Discord's native author slot); the
    # title carries no "orca_auto Flow" prefix and the transition uses a "→"
    # arrow. Field values wrap their code spans in Markdown backticks.
    embed = _embed(_event(event_type="workflow_status_changed"))
    assert embed["title"] == "Status changed"
    assert embed["author"] == {"name": "orca_auto"}
    assert _fields(embed) == {
        "Workflow": "`wf1`",
        "Template": "`tmpl`",
        "Status": "`queued` → `running`",
        "Directory": "`-`",
    }


def test_advance_failed_render() -> None:
    embed = _embed(_event(event_type="workflow_advance_failed"))
    assert embed["title"] == "❌ Advance failed"
    assert _fields(embed) == {
        "Workflow": "`wf1`",
        "Template": "`tmpl`",
        "Reason": "`because`",
        "Directory": "`-`",
    }


def test_stage_status_render_escapes_special_chars() -> None:
    embed = _embed(
        _event(
            event_type="workflow_stage_completed",
            stage_id="s<1>",
            metadata={
                "engine": "orca",
                "task_kind": "opt",
                "stage_status": "completed",
                "previous_stage_status": "running",
            },
        )
    )
    fields = _fields(embed)
    assert embed["title"] == "✅ Stage completed"
    assert fields["Stage"] == "`s<1>`"
    assert fields["Task"] == "`orca/opt`"
    assert fields["Stage status"] == "`running` → `completed`"
    # The internal event-type enum is no longer surfaced as a field.
    assert "Event" not in fields


def test_handoff_render_has_two_transitions() -> None:
    embed = _embed(
        _event(
            event_type="workflow_stage_handoff_ready",
            stage_id="s1",
            metadata={
                "engine": "xtb",
                "task_kind": "path",
                "stage_status": "completed",
                "previous_stage_status": "running",
                "reaction_handoff_status": "ready",
                "previous_reaction_handoff_status": "pending",
            },
        )
    )
    fields = _fields(embed)
    assert fields["Stage status"] == "`running` → `completed`"
    assert fields["Reaction handoff"] == "`pending` → `ready`"


def test_worker_lifecycle_render() -> None:
    # The title already names the worker event, so no redundant "Event" field.
    embed = _embed(_event(event_type="worker_started"))
    assert embed["title"] == "Worker started"
    assert embed["author"] == {"name": "orca_auto"}
    assert _fields(embed) == {
        "Workflow root": "`/nonexistent/orca-auto-test-wfroot`",
        "Worker session": "`sess<1>`",
        "Reason": "`because`",
    }


def test_default_event_render() -> None:
    # Unknown event types keep the "Event" field: the generic title alone does
    # not say what happened.
    embed = _embed(_event(event_type="something_else"))
    assert embed["title"] == "Workflow event"
    assert _fields(embed)["Event"] == "`something_else`"


def test_phase_finished_render_conditionals() -> None:
    embed = _embed(
        _event(
            event_type=WORKFLOW_PHASE_FINISHED_EVENT,
            metadata={
                "phase_label": "xTB",
                "phase_outcome": "completed",
                "stage_count": "2",
                "stage_status_counts": {"completed": 2},
                "stage_statuses": [{"label": "s1", "status": "completed"}],
                "reaction_handoff_status_counts": {"ready": 1},
                "failure_reasons": ["none"],
            },
        )
    )
    fields = _fields(embed)
    assert fields["Phase"] == "`xTB`"
    assert fields["Stage status counts"] == "`completed:2`"
    assert fields["Reaction handoff counts"] == "`ready:1`"
    assert fields["Failure reasons"] == "`none`"


def test_severity_maps_from_event_type() -> None:
    assert (
        notifications.journal_event_message(
            _event(event_type="workflow_advance_failed"), _ROOT
        ).severity
        == "error"
    )
    assert (
        notifications.journal_event_message(
            _event(event_type="workflow_stage_completed", metadata={"engine": "orca"}), _ROOT
        ).severity
        == "success"
    )


def test_workflow_event_renders_workspace_directory(tmp_path) -> None:
    """Workflow-scoped notifications show the workspace directory (the
    generation inside its scaffold), mirroring standalone ORCA's Directory
    field, instead of the worker session token."""

    scaffold = tmp_path / "rxn_case"
    workspace = scaffold / "20260717-104500-0a1b2c3d"
    workspace.mkdir(parents=True)
    (scaffold / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")
    (workspace / "workflow.json").write_text(
        '{"workflow_id": "20260717-104500-0a1b2c3d"}', encoding="utf-8"
    )

    embed = render_discord_embed(
        notifications.journal_event_message(
            _event(event_type="workflow_status_changed", workflow_id=workspace.name),
            tmp_path,
        )
    )
    fields = _fields(embed)

    assert fields["Directory"] == f"`{workspace}`"
    assert "Worker session" not in fields


@pytest.mark.parametrize("event_type", ["worker_cycle_started", "worker_cycle_finished"])
def test_worker_cycle_events_keep_their_session(tmp_path: Path, event_type: str) -> None:
    event = {
        "event_type": event_type,
        "worker_session_id": "wf_worker_20260717_074850_deadbeef",
        "metadata": {"cycle_started_at": "2026-07-17T10:00:00+00:00"},
    }

    rendered = str(render_discord_embed(notifications.journal_event_message(event, tmp_path)))

    assert "wf_worker_20260717_074850_deadbeef" in rendered


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
        notifications.journal_event_message(event, "/nonexistent/orca-auto-test-root_3")
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

    assert notifications.notification_event_types_from_env() == set(
        notifications.DEFAULT_NOTIFICATION_EVENT_TYPES
    )
    assert notifications.journal_notification_enabled("workflow_status_changed") is True
    assert notifications.journal_notification_enabled("workflow_stage_submitted") is False
    assert notifications.journal_notification_enabled("workflow_stage_handoff_ready") is False
    assert notifications.journal_notification_enabled("workflow_phase_finished") is False
    assert notifications.messenger_channel_from_env() is None

    monkeypatch.setenv(
        "ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES",
        "custom_event, workflow_status_changed, workflow_stage_submitted",
    )
    monkeypatch.setenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", "true")
    assert notifications.notification_event_types_from_env() == {
        "custom_event",
        "workflow_stage_submitted",
        "workflow_status_changed",
    }
    assert notifications.journal_notification_enabled("custom_event") is False

    monkeypatch.setenv("ORCA_AUTO_FLOW_NOTIFY_DISABLED", "0")
    assert notifications.journal_notification_enabled("custom_event") is True
    assert notifications.messenger_channel_from_env() is None


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

    channel = notifications.messenger_channel_from_env()

    assert isinstance(channel, DiscordBotChannel)
    assert channel.config.bot_token == "config-bot-token"
