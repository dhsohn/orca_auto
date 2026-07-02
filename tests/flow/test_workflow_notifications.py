from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.notifications import MAX_TELEGRAM_MESSAGE_LENGTH, TelegramSendResult
from orca_auto.flow.workflow import notifications as workflow_notifications


class _FakeTransport:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.parse_modes: list[str | None] = []

    def send_text(self, text: str, *, parse_mode: str | None = None) -> TelegramSendResult:
        self.messages.append(text)
        self.parse_modes.append(parse_mode)
        return TelegramSendResult(sent=True)


def _write_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "telegram:",
                "  bot_token: token",
                "  chat_id: chat",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_maybe_notify_workflow_phase_summary_sends_crest_summary_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    _write_config(config_path)
    transport = _FakeTransport()
    monkeypatch.setattr(workflow_notifications, "build_telegram_transport", lambda _cfg: transport)
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
        payload=payload,
        config_path=str(config_path),
        phase_engine="crest",
    )
    assert not workflow_notifications.maybe_notify_workflow_phase_summary(
        payload=payload,
        config_path=str(config_path),
        phase_engine="crest",
    )
    assert len(transport.messages) == 1
    assert transport.parse_modes == ["HTML"]
    message = transport.messages[0]
    assert "<b>orca_auto Flow CREST Phase Summary</b>" in message
    assert "<b>Stages</b>: <code>2</code>" in message
    assert "<b>Stage</b>: reactant" in message
    assert "<b>Retained conformers</b>: <code>2</code>" in message
    assert "<b>Stage</b>: product" in message
    assert "<b>Retained conformers</b>: <code>0</code>" in message
    assert payload["metadata"]["phase_notifications"]["crest_summary"]["sent_at"]


def test_maybe_notify_workflow_phase_summary_sends_xtb_ready_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    _write_config(config_path)
    transport = _FakeTransport()
    monkeypatch.setattr(workflow_notifications, "build_telegram_transport", lambda _cfg: transport)
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
        config_path=str(config_path),
        phase_engine="xtb",
        extra_lines=["planned_orca_stages: 1"],
    )

    assert len(transport.messages) == 1
    assert transport.parse_modes == ["HTML"]
    message = transport.messages[0]
    assert "<b>orca_auto Flow xTB Phase Summary</b>" in message
    assert "wf_&lt;xtb&gt;_1" in message
    assert "<b>Ready for ORCA</b>: <code>1</code>" in message
    assert "<b>planned_orca_stages</b>: <code>1</code>" in message
    assert "<b>Stage</b>: rxn_&lt;01&gt;" in message
    assert "<b>Handoff</b>: <code>ready</code>" in message
    assert "<b>Candidates</b>: <code>3</code>" in message
    assert "<b>Stage</b>: rxn_02" in message
    assert "<b>Handoff</b>: <code>failed</code>" in message
    assert "<b>Candidates</b>: <code>0</code>" in message


def test_maybe_notify_workflow_phase_summary_splits_long_messages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    _write_config(config_path)
    transport = _FakeTransport()
    monkeypatch.setattr(workflow_notifications, "build_telegram_transport", lambda _cfg: transport)
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
        config_path=str(config_path),
        phase_engine="crest",
        extra_lines=[f"note_{index}: {'x' * 60}" for index in range(120)],
    )

    assert len(transport.messages) > 1
    assert all(len(message) <= MAX_TELEGRAM_MESSAGE_LENGTH for message in transport.messages)
    assert all(parse_mode == "HTML" for parse_mode in transport.parse_modes)
