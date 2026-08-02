"""ORCA lifecycle notification tests.

Message-builder metadata (title/severity) and ``notify_*`` delivery are
exercised through a fake :class:`MessageChannel`, plus a Discord end-to-end.
"""

from __future__ import annotations

from typing import Literal

from orca_auto.core.messaging import Message, SendResult
from orca_auto.orca.notifications import (
    notify_queue_enqueued_event,
    notify_retry_event,
    notify_run_finished_event,
    notify_run_started_event,
    run_finished_message,
)
from orca_auto.orca.types import (
    QueueEnqueuedNotification,
    RetryNotification,
    RunFinishedNotification,
    RunStartedNotification,
)


# --------------------------------------------------------------------------- #
# Sample events
# --------------------------------------------------------------------------- #
def _started_event() -> RunStartedNotification:
    return {
        "reaction_dir": "/tmp/rxn<demo>",
        "selected_inp": "/tmp/rxn<demo>/rxn.inp",
        "current_inp": "/tmp/rxn<demo>/rxn.inp",
        "run_id": "run_x",
        "attempt_index": 1,
        "max_retries": 2,
        "status": "running",
        "attempt_started_at": "2026-03-10T00:00:00+00:00",
        "resumed": False,
    }


def _retry_event() -> RetryNotification:
    return {
        "reaction_dir": "/tmp/rxn<demo>",
        "selected_inp": "/tmp/rxn<demo>/rxn.inp",
        "failed_inp": "/tmp/rxn<demo>/rxn.inp",
        "failed_out": "/tmp/rxn<demo>/rxn.out",
        "next_inp": "/tmp/rxn<demo>/rxn.retry01.inp",
        "attempt_index": 1,
        "retry_number": 1,
        "max_retries": 2,
        "analyzer_status": "error_scf",
        "analyzer_reason": "scf_not_converged",
        "patch_actions": ["route_add_tightscf_slowconv", "geometry_restart_from_rxn.xyz"],
        "resumed": False,
    }


def _finished_event() -> RunFinishedNotification:
    return {
        "reaction_dir": "/tmp/rxn<demo>",
        "selected_inp": "/tmp/rxn<demo>/rxn.inp",
        "run_id": "run_x",
        "status": "completed",
        "analyzer_status": "completed",
        "reason": "normal_termination",
        "attempt_count": 2,
        "max_retries": 2,
        "completed_at": "2026-03-10T00:05:00+00:00",
        "last_out_path": "/tmp/rxn<demo>/rxn.retry01.out",
        "resumed": False,
        "skipped_execution": False,
    }


def _queued_event() -> QueueEnqueuedNotification:
    return {
        "queue_id": "q<1>",
        "reaction_dir": "/tmp/rxn<demo>",
        "priority": 5,
        "force": True,
        "enqueued_at": "2026-03-10T00:00:00+00:00",
    }


def test_run_finished_cancelled_title_and_severity() -> None:
    event = _finished_event()
    event["status"] = "cancelled"
    event["reason"] = "cancel_requested"
    message = run_finished_message(event)
    assert message.title == "ORCA cancelled"
    assert message.severity == "warning"


# --------------------------------------------------------------------------- #
# Severity / embed-facing metadata
# --------------------------------------------------------------------------- #
def test_run_finished_completed_is_success_severity() -> None:
    assert run_finished_message(_finished_event()).severity == "success"


class _FakeChannel:
    def __init__(self, *, enabled: bool = True, sent: bool = True) -> None:
        self._enabled = enabled
        self._sent = sent
        self.sent_messages: list[Message] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        self.sent_messages.append(message)
        return SendResult(sent=self._sent)


def test_notify_run_started_delivers_message() -> None:
    channel = _FakeChannel()
    assert notify_run_started_event(channel, _started_event()) is True
    assert len(channel.sent_messages) == 1
    assert channel.sent_messages[0].title == "ORCA started"


def test_notify_skips_when_channel_disabled() -> None:
    channel = _FakeChannel(enabled=False)
    assert notify_run_started_event(channel, _started_event()) is False
    assert notify_retry_event(channel, _retry_event()) is False
    assert notify_run_finished_event(channel, _finished_event()) is False
    assert notify_queue_enqueued_event(channel, _queued_event()) is False
    assert channel.sent_messages == []


def test_notify_returns_false_when_send_fails() -> None:
    channel = _FakeChannel(sent=False)
    assert notify_run_finished_event(channel, _finished_event()) is False
    assert len(channel.sent_messages) == 1


def test_discord_provider_end_to_end_posts_embed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """provider=discord config -> build_channel -> notify_* -> bot embed POST."""
    import json

    from orca_auto.core.config import DiscordConfig, MessengerConfig
    from orca_auto.core.messaging import build_channel
    from orca_auto.core.messaging import discord_bot as discord_mod

    posted: dict[str, bytes] = {}

    class _Resp:
        status = 200

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b'{"id":"999"}'

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> Literal[False]:
            return False

    def fake_urlopen(request: object, timeout: float) -> _Resp:
        posted["data"] = request.data  # type: ignore[attr-defined]
        return _Resp()

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = build_channel(
        MessengerConfig(
            provider="discord",
            discord=DiscordConfig(bot_token="test-bot-token", default_channel_id="123"),
        )
    )
    assert notify_run_started_event(channel, _started_event()) is True

    embed = json.loads(posted["data"])["embeds"][0]
    assert embed["title"] == "ORCA started"
    assert embed["author"] == {"name": "orca_auto"}
    field_names = [field["name"] for field in embed["fields"]]
    assert "Job" in field_names
    assert "Attempt" in field_names
