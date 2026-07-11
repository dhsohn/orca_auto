"""ORCA lifecycle notification tests.

The Telegram renders are asserted byte-for-byte against the HTML the previous
``telegram_notifier`` module produced, proving the Doc-model migration is
behaviour-preserving for the Telegram path. ``notify_*`` delivery is exercised
through a fake :class:`MessageChannel`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from orca_auto.core.messaging import Message, SendResult, render_telegram
from orca_auto.orca.dft.monitor import MonitorResult, ScanReport
from orca_auto.orca.notifications import (
    _status_icon,
    has_monitor_updates,
    monitor_message,
    notify_monitor_report,
    notify_queue_enqueued_event,
    notify_retry_event,
    notify_run_finished_event,
    notify_run_started_event,
    queue_enqueued_message,
    retry_message,
    run_finished_message,
    run_started_message,
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


def _sample_report() -> ScanReport:
    return ScanReport(
        new_results=[
            MonitorResult(
                formula="CH4",
                method_basis="B3LYP/def2-SVP",
                energy="E = -40.5 Eh",
                status="completed",
                calc_type="opt",
                path="orca_outputs/opt/CH4/calc.out",
                note="",
            ),
            MonitorResult(
                formula="C6H6",
                method_basis="PBE0/def2-TZVP",
                energy="E = -232.1 Eh",
                status="failed",
                calc_type="opt+freq",
                path="orca_outputs/opt/C6H6/calc.out",
                note=" (NOT CONVERGED)",
            ),
        ],
        scanned_files=10,
    )


# --------------------------------------------------------------------------- #
# Golden Telegram renders (byte-identical to the pre-migration output)
# --------------------------------------------------------------------------- #
_GOLDEN_STARTED = (
    "<b>orca_auto ORCA Started</b>\n"
    "<b>Job</b>: rxn&lt;demo&gt;\n"
    "<b>Attempt</b>: #1 (<code>running</code>)\n"
    "<b>Input</b>: <code>rxn.inp</code>\n"
    "<b>Max retries</b>: 2\n"
    "<b>Directory</b>: <code>/tmp/rxn&lt;demo&gt;</code>"
)

_GOLDEN_RETRY = (
    "<b>orca_auto ORCA Retry</b>\n"
    "<b>Job</b>: rxn&lt;demo&gt;\n"
    "<b>Attempt</b>: 1 failed; retry 1/2 is starting\n"
    "<b>Reason</b>: <code>error_scf</code> (scf_not_converged)\n"
    "<b>Failed input</b>: <code>rxn.inp</code>\n"
    "<b>Restart input</b>: <code>rxn.retry01.inp</code>\n"
    "<b>Applied patches</b>: TightSCF + SlowConv, geometry restart from rxn.xyz\n"
    "<b>Directory</b>: <code>/tmp/rxn&lt;demo&gt;</code>"
)

_GOLDEN_FINISHED = (
    "<b>orca_auto ORCA Completed</b>\n"
    "<b>Job</b>: rxn&lt;demo&gt;\n"
    "<b>Result</b>: <code>completed</code>\n"
    "<b>Attempts</b>: 2\n"
    "<b>Reason</b>: <code>normal_termination</code>\n"
    "<b>Analyzer</b>: <code>completed</code>\n"
    "<b>Output</b>: <code>rxn.retry01.out</code>\n"
    "<b>Directory</b>: <code>/tmp/rxn&lt;demo&gt;</code>"
)

_GOLDEN_QUEUED = (
    "<b>orca_auto ORCA Queued</b>\n"
    "<b>Job</b>: rxn&lt;demo&gt;\n"
    "<b>Queue ID</b>: <code>q&lt;1&gt;</code>\n"
    "<b>Priority</b>: 5\n"
    "<b>Mode</b>: force re-enqueue\n"
    "<b>Directory</b>: <code>/tmp/rxn&lt;demo&gt;</code>"
)

_GOLDEN_MONITOR = (
    "⚙️ <b>orca_auto scan-notify</b>  <code>2026-03-10 12:00 UTC</code>\n\n" + "─" * 28 + "\n\n"
    "\U0001f50d <b>Scope</b>\n"
    "Filesystem discovery only. Use run-dir alerts for immediate lifecycle events.\n\n"
    "\U0001f9ea <b>New Calculations Detected</b>  (2)\n\n"
    "✅ <b>CH4</b>  [OPT]\n"
    "   \U0001f9ec B3LYP/def2-SVP\n"
    "   ⚡ E = -40.5 Eh\n"
    "   \U0001f4c2 <code>orca_outputs/opt/CH4/calc.out</code>\n\n"
    "❌ <b>C6H6</b>  [OPT+FREQ]\n"
    "   \U0001f9ec PBE0/def2-TZVP\n"
    "   ⚡ E = -232.1 Eh\n"
    "   \U0001f4c2 <code>orca_outputs/opt/C6H6/calc.out</code>\n"
    "   ⚠️ NOT CONVERGED"
)


def test_run_started_render_is_byte_identical() -> None:
    assert render_telegram(run_started_message(_started_event())) == _GOLDEN_STARTED


def test_run_started_resumed_render() -> None:
    event = _started_event()
    event["resumed"] = True
    event["status"] = ""
    rendered = render_telegram(run_started_message(event))
    assert "<b>orca_auto ORCA Resumed</b>" in rendered
    assert "<b>Mode</b>: resumed run" in rendered


def test_retry_render_is_byte_identical() -> None:
    assert render_telegram(retry_message(_retry_event())) == _GOLDEN_RETRY


def test_run_finished_render_is_byte_identical() -> None:
    assert render_telegram(run_finished_message(_finished_event())) == _GOLDEN_FINISHED


def test_run_finished_cancelled_title_and_severity() -> None:
    event = _finished_event()
    event["status"] = "cancelled"
    event["reason"] = "cancel_requested"
    message = run_finished_message(event)
    assert message.title == "orca_auto ORCA Cancelled"
    assert message.severity == "warning"


def test_queue_enqueued_render_is_byte_identical() -> None:
    assert render_telegram(queue_enqueued_message(_queued_event())) == _GOLDEN_QUEUED


def test_monitor_render_is_byte_identical() -> None:
    report = _sample_report()
    rendered = render_telegram(
        monitor_message(report, now=datetime(2026, 3, 10, 12, 0, tzinfo=UTC))
    )
    assert rendered == _GOLDEN_MONITOR


# --------------------------------------------------------------------------- #
# Severity / embed-facing metadata
# --------------------------------------------------------------------------- #
def test_run_finished_completed_is_success_severity() -> None:
    assert run_finished_message(_finished_event()).severity == "success"


def test_status_icon() -> None:
    assert _status_icon("completed") == "✅"
    assert _status_icon("failed") == "❌"
    assert _status_icon("unknown") == "•"


# --------------------------------------------------------------------------- #
# Delivery through a channel
# --------------------------------------------------------------------------- #
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
    assert channel.sent_messages[0].title == "orca_auto ORCA Started"


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


def test_notify_monitor_report_skips_empty_report() -> None:
    channel = _FakeChannel()
    assert notify_monitor_report(channel, ScanReport(new_results=[], scanned_files=0)) is False
    assert channel.sent_messages == []


def test_notify_monitor_report_sends_when_updates_exist() -> None:
    channel = _FakeChannel()
    report = _sample_report()
    assert has_monitor_updates(report) is True
    assert notify_monitor_report(channel, report) is True
    assert len(channel.sent_messages) == 1


def test_discord_provider_end_to_end_posts_embed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """provider=discord config -> build_channel -> notify_* -> webhook embed POST."""
    import json

    from orca_auto.core.config import DiscordConfig, MessengerConfig
    from orca_auto.core.messaging import build_channel
    from orca_auto.core.messaging import discord_webhook as discord_mod

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
            discord=DiscordConfig(webhook_url="https://discord.com/api/webhooks/123/test-token"),
        )
    )
    assert notify_run_started_event(channel, _started_event()) is True

    embed = json.loads(posted["data"])["embeds"][0]
    assert embed["title"] == r"orca\_auto ORCA Started"
    field_names = [field["name"] for field in embed["fields"]]
    assert "Job" in field_names
    assert "Attempt" in field_names
