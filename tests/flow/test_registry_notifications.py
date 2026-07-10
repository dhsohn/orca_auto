"""Registry journal-event notification tests (Doc-model renders)."""

from __future__ import annotations

from typing import Any

from orca_auto.core.messaging import render_telegram
from orca_auto.flow.registry import _notifications as notifications
from orca_auto.flow.workflow._phases import WORKFLOW_PHASE_FINISHED_EVENT

_ROOT = "/tmp/wfroot"


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
    # The ``->`` separator is HTML-escaped now (valid HTML; Telegram shows "->").
    assert _render(_event(event_type="workflow_status_changed")) == (
        "<b>orca_auto Flow Status Changed</b>\n"
        "<b>Workflow</b>: <code>wf1</code>\n"
        "<b>Template</b>: <code>tmpl</code>\n"
        "<b>Status</b>: <code>queued</code> -&gt; <code>running</code>\n"
        "<b>Worker session</b>: <code>sess&lt;1&gt;</code>"
    )


def test_advance_failed_render() -> None:
    assert _render(_event(event_type="workflow_advance_failed")) == (
        "<b>orca_auto Flow Advance Failed</b>\n"
        "<b>Workflow</b>: <code>wf1</code>\n"
        "<b>Template</b>: <code>tmpl</code>\n"
        "<b>Reason</b>: <code>because</code>\n"
        "<b>Worker session</b>: <code>sess&lt;1&gt;</code>"
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
    assert "<b>orca_auto Flow Stage Completed</b>" in rendered
    assert "<b>Stage</b>: <code>s&lt;1&gt;</code>" in rendered
    assert "<b>Task</b>: <code>orca/opt</code>" in rendered
    assert "<b>Stage status</b>: <code>running</code> -&gt; <code>completed</code>" in rendered


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
    assert "<b>Reaction handoff</b>: <code>pending</code> -&gt; <code>ready</code>" in rendered


def test_worker_lifecycle_render() -> None:
    assert _render(_event(event_type="worker_started")) == (
        "<b>orca_auto Flow Worker Started</b>\n"
        "<b>Event</b>: <code>worker_started</code>\n"
        "<b>Workflow root</b>: <code>/tmp/wfroot</code>\n"
        "<b>Worker session</b>: <code>sess&lt;1&gt;</code>\n"
        "<b>Reason</b>: <code>because</code>"
    )


def test_default_event_render() -> None:
    rendered = _render(_event(event_type="something_else"))
    assert rendered.startswith("<b>orca_auto Flow Event</b>")
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
