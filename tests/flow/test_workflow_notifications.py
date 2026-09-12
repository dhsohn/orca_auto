from __future__ import annotations

from pathlib import Path
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


def test_maybe_notify_workflow_phase_summary_sends_crest_summary_once(
    monkeypatch: Any, tmp_path: Path
) -> None:
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    # Three conformers in one ensemble file. The count must come from the file,
    # not from the number of ensemble files, which the engine caps at four.
    conformers = tmp_path / "crest_conformers.xyz"
    conformers.write_text(
        "".join(f"2\nframe {index}\nH 0.0 0.0 0.0\nH 0.0 0.0 {index}.0\n" for index in range(3)),
        encoding="utf-8",
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_crest_1",
        "template_name": "conformer_screening",
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
                "output_artifacts": [
                    {"kind": "crest_conformer", "path": str(conformers)},
                    {"kind": "crest_conformer", "path": str(tmp_path / "crest_best.xyz")},
                ],
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
    # Two ensemble files, three conformers — the old metric reported `2` here.
    assert "**Conformers**: `3`" in description
    assert "**Stage**: product" in description
    # A stage with no ensemble file reports no count rather than a false zero.
    assert "**Conformers**: `-`" in description
    assert payload["metadata"]["phase_notifications"]["crest_summary"]["sent_at"]


def test_maybe_notify_workflow_phase_summary_names_a_refused_crest_ensemble(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # A refused ensemble file leaves no output artifact and need not move the
    # conformer count, so the Conformers metric alone cannot show the loss.
    channel = _RecordingChannel()
    _patch_channel(monkeypatch, channel)
    rotamers = tmp_path / "crest_rotamers.xyz"
    rotamers.write_text(
        "".join(f"2\nframe {index}\nH 0.0 0.0 0.0\nH 0.0 0.0 {index}.0\n" for index in range(3)),
        encoding="utf-8",
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_crest_refused",
        "template_name": "conformer_screening",
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
                "metadata": {
                    "crest_rejected_retained_outputs": [
                        {"name": "crest_conformers.xyz", "reason": "no_valid_frames"}
                    ],
                    "crest_no_primary_ensemble_retained": True,
                },
                "output_artifacts": [{"kind": "crest_conformer", "path": str(rotamers)}],
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "task": {
                    "engine": "crest",
                    "status": "completed",
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
    description = _description(channel.messages[0])
    stage_blocks = description.split("**Stage**: ")
    assert "**Refused**: `crest_conformers.xyz`" in stage_blocks[1]
    # The stage that refused nothing does not grow an empty metric.
    assert "Refused" not in stage_blocks[2]


def test_maybe_notify_workflow_phase_summary_skips_when_channel_disabled(monkeypatch: Any) -> None:
    channel = _RecordingChannel(enabled=False)
    _patch_channel(monkeypatch, channel)
    payload: dict[str, Any] = {
        "workflow_id": "wf_crest_disabled",
        "template_name": "conformer_screening",
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
        "template_name": "conformer_screening",
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
        "template_name": "conformer_screening",
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
