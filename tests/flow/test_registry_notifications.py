"""Registry journal-event notification tests (Doc-model renders)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.messaging import render_discord_embed
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
