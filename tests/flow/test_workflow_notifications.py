from __future__ import annotations

from typing import Any

import pytest

from orca_auto.core.messaging import Message, SendResult, render_discord_embed
from orca_auto.core.messaging.richtext import Field
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


def _fields(message: Message) -> dict[str, str]:
    embed = render_discord_embed(message)
    return {item["name"]: item["value"] for item in embed.get("fields", [])}


def _description(message: Message) -> str:
    return render_discord_embed(message).get("description", "")


def _field_labels(message: Message) -> list[str]:
    return [
        item.label for group in message.groups for item in group.items if isinstance(item, Field)
    ]


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
    message = channel.messages[0]
    embed = render_discord_embed(message)
    fields = _fields(message)
    description = _description(message)
    assert embed["author"] == {"name": "orca_auto"}
    assert "CREST phase summary" in embed["title"]
    assert fields["Stages"].startswith("`2`")
    assert "**Stage**: reactant" in description
    assert "**Retained conformers**: `2`" in description
    assert "**Stage**: product" in description
    assert "**Retained conformers**: `0`" in description
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
    message = channel.messages[0]
    embed = render_discord_embed(message)
    fields = _fields(message)
    description = _description(message)
    assert "xTB phase summary" in embed["title"]
    assert fields["Workflow"] == "`wf_<xtb>_1`"
    assert fields["Ready for ORCA"] == "`1`"
    assert fields["planned_orca_stages"] == "`1`"
    assert r"**Stage**: rxn\_\<01\>" in description
    assert "**Handoff**: `ready`" in description
    assert "**Candidates**: `3`" in description
    assert r"**Stage**: rxn\_02" in description
    assert "**Handoff**: `failed`" in description
    assert "**Candidates**: `0`" in description


def test_maybe_notify_workflow_phase_summary_reports_exhausted_handoffs_as_failed(
    monkeypatch: Any,
) -> None:
    """Stages whose jobs completed but whose ORCA handoff was refused are failures.

    Mirrors the observed reaction_ts_search shape where every xTB path search
    exited 0 (stage/task status "completed") while no stage produced a usable
    ts_guess, so the workflow itself terminal-failed.
    """
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_xtb_handoff_exhausted",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": f"xtb_path_search_{index:02d}",
                "status": "completed",
                "task": {
                    "engine": "xtb",
                    "status": "completed",
                    "payload": {"reaction_key": f"rxn_{index:02d}"},
                },
                "metadata": {
                    "reaction_handoff_status": "failed",
                    "reaction_handoff_reason": "xtb_ts_guess_missing",
                    "xtb_attempts": [{"attempt_number": 2, "candidate_count": 0}],
                },
                "output_artifacts": [],
            }
            for index in range(1, 10)
        ],
    }

    assert workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload, config_path="cfg", phase_engine="xtb"
    )

    assert len(channel.messages) == 1
    message = channel.messages[0]
    assert message.severity == "error"
    fields = _fields(message)
    description = _description(message)
    assert fields["Outcome"] == "`failed`"
    assert "completed=`0`" in fields["Stages"]
    assert "failed=`9`" in fields["Stages"]
    assert fields["Ready for ORCA"] == "`0`"
    assert description.count("**Result**: `failed`") == 9


def test_maybe_notify_workflow_phase_summary_treats_handoff_ready_failure_as_completed(
    monkeypatch: Any,
) -> None:
    """A failed retry after a successful handoff must not mask the delivered result."""
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_xtb_recoverable",
        "template_name": "reaction_ts_search",
        "metadata": {},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "failed",
                "task": {
                    "engine": "xtb",
                    "status": "failed",
                    "payload": {"reaction_key": "rxn_01"},
                },
                "metadata": {
                    "reaction_handoff_status": "ready",
                    "xtb_attempts": [{"attempt_number": 1, "candidate_count": 2}],
                },
                "output_artifacts": [],
            },
        ],
    }

    assert workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload, config_path="cfg", phase_engine="xtb"
    )

    assert len(channel.messages) == 1
    message = channel.messages[0]
    assert message.severity == "success"
    fields = _fields(message)
    description = _description(message)
    assert fields["Outcome"] == "`completed`"
    assert "**Result**: `completed`" in description
    assert fields["Ready for ORCA"] == "`1`"


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
    labels = _field_labels(channel.messages[0])
    # All 120 notes are present in the built message; the channel is responsible
    # for chunking or truncating at delivery time.
    assert "note_0" in labels
    assert "note_119" in labels


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
