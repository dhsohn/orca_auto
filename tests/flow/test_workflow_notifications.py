from __future__ import annotations

from typing import Any

import pytest

from orca_auto.core.messaging import Message, SendResult, render_telegram
from orca_auto.flow.workflow import notifications as workflow_notifications


class _RecordingChannel:
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.messages: list[Message] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        self.messages.append(message)
        return SendResult(sent=True)


def _patch_channel(monkeypatch: Any, channel: _RecordingChannel) -> None:
    monkeypatch.setattr(
        workflow_notifications, "build_channel_from_config_path", lambda *_a, **_k: channel
    )


def test_maybe_notify_workflow_phase_summary_sends_crest_summary_once(monkeypatch: Any) -> None:
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_crest_1",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "task": {
                    "engine": "crest",
                    "status": "completed",
                    "payload": {"input_role": "reactant"},
                },
                "metadata": {},
                "output_artifacts": [{"path": "a.xyz"}, {"path": "b.xyz"}],
            },
            {
                "stage_id": "crest_product",
                "status": "failed",
                "task": {
                    "engine": "crest",
                    "status": "failed",
                    "payload": {"input_role": "product"},
                },
                "metadata": {},
                "output_artifacts": [],
            },
        ],
    }

    assert workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload, config_path="cfg", phase_engine="crest"
    )
    assert not workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload, config_path="cfg", phase_engine="crest"
    )
    assert len(channel.messages) == 1
    rendered = render_telegram(channel.messages[0])
    assert "<b>CREST phase summary</b>" in rendered
    assert "<b>Stages</b>: <code>2</code>" in rendered
    assert "<b>Stage</b>: reactant" in rendered
    assert "<b>Retained conformers</b>: <code>2</code>" in rendered
    assert "<b>Stage</b>: product" in rendered
    assert "<b>Retained conformers</b>: <code>0</code>" in rendered
    assert payload["metadata"]["phase_notifications"]["crest_summary"]["sent_at"]


def test_maybe_notify_workflow_phase_summary_sends_xtb_ready_counts(monkeypatch: Any) -> None:
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_<xtb>_1",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "task": {
                    "engine": "xtb",
                    "status": "completed",
                    "payload": {"reaction_key": "rxn_<01>"},
                },
                "metadata": {
                    "reaction_handoff_status": "ready",
                    "xtb_attempts": [{"attempt_number": 0, "candidate_count": 3}],
                },
                "output_artifacts": [],
            },
            {
                "stage_id": "xtb_path_search_02",
                "status": "failed",
                "task": {
                    "engine": "xtb",
                    "status": "failed",
                    "payload": {"reaction_key": "rxn_02"},
                },
                "metadata": {
                    "reaction_handoff_status": "failed",
                    "xtb_attempts": [{"attempt_number": 0, "candidate_count": 0}],
                },
                "output_artifacts": [],
            },
        ],
    }

    assert workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload,
        config_path="cfg",
        phase_engine="xtb",
        extra_lines=["planned_orca_stages: 1"],
    )

    assert len(channel.messages) == 1
    rendered = render_telegram(channel.messages[0])
    assert "<b>xTB phase summary</b>" in rendered
    assert "wf_&lt;xtb&gt;_1" in rendered
    assert "<b>Ready for ORCA</b>: <code>1</code>" in rendered
    assert "<b>planned_orca_stages</b>: <code>1</code>" in rendered
    assert "<b>Stage</b>: rxn_&lt;01&gt;" in rendered
    assert "<b>Handoff</b>: <code>ready</code>" in rendered
    assert "<b>Candidates</b>: <code>3</code>" in rendered
    assert "<b>Stage</b>: rxn_02" in rendered
    assert "<b>Handoff</b>: <code>failed</code>" in rendered
    assert "<b>Candidates</b>: <code>0</code>" in rendered


def test_maybe_notify_workflow_phase_summary_skips_when_channel_disabled(monkeypatch: Any) -> None:
    channel = _RecordingChannel(enabled=False)
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_crest_disabled",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "task": {
                    "engine": "crest",
                    "status": "completed",
                    "payload": {"input_role": "reactant"},
                },
                "metadata": {},
                "output_artifacts": [{"path": "a.xyz"}],
            },
        ],
    }

    assert not workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload, config_path="cfg", phase_engine="crest"
    )
    assert channel.messages == []
    assert "phase_notifications" not in payload["metadata"] or not payload["metadata"][
        "phase_notifications"
    ].get("crest_summary")


def test_maybe_notify_workflow_phase_summary_includes_all_notes(monkeypatch: Any) -> None:
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_crest_long",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "task": {
                    "engine": "crest",
                    "status": "completed",
                    "payload": {"input_role": "reactant"},
                },
                "metadata": {},
                "output_artifacts": [{"path": "a.xyz"}],
            },
        ],
    }

    assert workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload,
        config_path="cfg",
        phase_engine="crest",
        extra_lines=[f"note_{index}: {'x' * 60}" for index in range(120)],
    )

    assert len(channel.messages) == 1
    rendered = render_telegram(channel.messages[0])
    # All 120 notes are present; the Telegram channel is responsible for chunking.
    assert "<b>note_0</b>:" in rendered
    assert "<b>note_119</b>:" in rendered


@pytest.mark.parametrize("failure_site", ["factory", "send"])
def test_phase_notification_failure_does_not_fail_workflow_advance(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
    failure_site: str,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_safe",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "task": {
                    "engine": "crest",
                    "status": "completed",
                    "payload": {"input_role": "reactant"},
                },
                "metadata": {},
                "output_artifacts": [{"path": "a.xyz"}],
            }
        ],
    }

    if failure_site == "factory":
        monkeypatch.setattr(
            workflow_notifications,
            "build_channel_from_config_path",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad provider")),
        )
    else:
        channel = _RecordingChannel()
        monkeypatch.setattr(
            channel,
            "send",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("wire failure")),
        )
        _patch_channel(monkeypatch, channel)

    with caplog.at_level("WARNING"):
        assert not workflow_notifications.maybe_notify_workflow_phase_summary(
            payload=payload,
            config_path="cfg",
            phase_engine="crest",
        )

    assert "workflow_phase_notification_failed" in caplog.text
    assert "phase_notifications" not in payload["metadata"] or not payload["metadata"][
        "phase_notifications"
    ].get("crest_summary")
