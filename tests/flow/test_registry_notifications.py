"""Registry journal-event notification tests (Doc-model renders)."""

from __future__ import annotations

from typing import Any

from orca_auto.core.messaging import render_telegram
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


def _render(event: dict[str, Any]) -> str:
    return render_telegram(notifications.journal_event_message(event, _ROOT))


def test_status_changed_render() -> None:
    # Identity renders as a leading author line (Telegram has no author slot);
    # the title no longer carries the "orca_auto Flow" prefix and the transition
    # uses a "→" arrow.
    assert _render(_event(event_type="workflow_status_changed")) == (
        "orca_auto\n"
        "<b>Status changed</b>\n"
        "<b>Workflow</b>: <code>wf1</code>\n"
        "<b>Template</b>: <code>tmpl</code>\n"
        "<b>Status</b>: <code>queued</code> → <code>running</code>\n"
        "<b>Directory</b>: <code>-</code>"
    )


def test_advance_failed_render() -> None:
    assert _render(_event(event_type="workflow_advance_failed")) == (
        "orca_auto\n"
        "<b>Advance failed</b>\n"
        "<b>Workflow</b>: <code>wf1</code>\n"
        "<b>Template</b>: <code>tmpl</code>\n"
        "<b>Reason</b>: <code>because</code>\n"
        "<b>Directory</b>: <code>-</code>"
    )


def test_stage_status_render_escapes_special_chars() -> None:
    rendered = _render(
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
    assert "<b>Stage completed</b>" in rendered
    assert "<b>Stage</b>: <code>s&lt;1&gt;</code>" in rendered
    assert "<b>Task</b>: <code>orca/opt</code>" in rendered
    assert "<b>Stage status</b>: <code>running</code> → <code>completed</code>" in rendered
    # The internal event-type enum is no longer surfaced as a field.
    assert "<b>Event</b>" not in rendered


def test_handoff_render_has_two_transitions() -> None:
    rendered = _render(
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
    assert "<b>Reaction handoff</b>: <code>pending</code> → <code>ready</code>" in rendered


def test_worker_lifecycle_render() -> None:
    # The title already names the worker event, so no redundant "Event" field.
    assert _render(_event(event_type="worker_started")) == (
        "orca_auto\n"
        "<b>Worker started</b>\n"
        "<b>Workflow root</b>: <code>/nonexistent/orca-auto-test-wfroot</code>\n"
        "<b>Worker session</b>: <code>sess&lt;1&gt;</code>\n"
        "<b>Reason</b>: <code>because</code>"
    )


def test_default_event_render() -> None:
    # Unknown event types keep the "Event" field: the generic title alone does
    # not say what happened.
    rendered = _render(_event(event_type="something_else"))
    assert rendered.startswith("orca_auto\n<b>Workflow event</b>")
    assert "<b>Event</b>: <code>something_else</code>" in rendered


def test_phase_finished_render_conditionals() -> None:
    rendered = _render(
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
    assert "<b>Phase</b>: <code>xTB</code>" in rendered
    assert "<b>Stage status counts</b>: <code>completed:2</code>" in rendered
    assert "<b>Reaction handoff counts</b>: <code>ready:1</code>" in rendered
    assert "<b>Failure reasons</b>: <code>none</code>" in rendered


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


def test_should_suppress_stage_notification() -> None:
    suppressed = _event(event_type="workflow_stage_completed", metadata={"engine": "crest"})
    assert notifications.should_suppress_stage_notification(suppressed) is True
    kept = _event(event_type="workflow_status_changed", metadata={"engine": "crest"})
    assert notifications.should_suppress_stage_notification(kept) is False


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

    rendered = render_telegram(
        notifications.journal_event_message(
            _event(event_type="workflow_status_changed", workflow_id=workspace.name),
            tmp_path,
        )
    )

    assert f"<b>Directory</b>: <code>{workspace}</code>" in rendered
    assert "Worker session" not in rendered
