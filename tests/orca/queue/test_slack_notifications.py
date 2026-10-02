"""Slack through the parent's real queued and terminal notification claims.

A synthetic ``orca_auto.yaml`` in ``tmp_path`` is read by the real
``load_config``. A queued row (``persistence.save_entries``) and a completed
``job_state.json`` (the real state writers) live under that temporary runs
root. ``notify_queued_jobs`` and ``claim_and_send_terminal`` run unpatched:
they resolve the real channel, write their durable claim, and hand delivery to
the real ``dispatch_notification`` background thread, which the tests join
with a bound. Only the lowest HTTP transport (``HTTPSHandler.https_open`` and
``HTTPHandler.http_open``) and ``time.sleep`` are replaced; sockets are
refused and proxies and the cached global opener are isolated.

Delivery stays best effort: one durable claim, at most one background send, no
retry after a failed send and no write after delivery.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from email.message import Message as EmailMessage
from io import BytesIO
from pathlib import Path
from urllib.response import addinfourl

import pytest

from orca_auto.core.queue import persistence
from orca_auto.orca import notifications as orca_notifications
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.orca.queue import notifications as queue_notifications
from orca_auto.orca.queue.adapter import list_queue
from orca_auto.orca.queue.entries import QUEUED_NOTIFICATION_PENDING_KEY
from orca_auto.orca.state_reading import load_state, state_path
from tests.conftest import make_queue_entry
from tests.core.messaging.test_slack_lifecycle import _write_config
from tests.queue_worker_helpers import write_completed_run_state

# Obviously synthetic values: no real Slack/Discord workspace, token or channel.
_SLACK_TOKEN = "xoxb-synthetic-queue-token"
_SLACK_CHANNEL = "C0SYNTHETIC1"
_SLACK_URL = "https://slack.com/api/chat.postMessage"
_DISCORD_TOKEN = "synthetic-discord-queue-token"
_DISCORD_CHANNEL = "123"
_DISCORD_URL = f"https://discord.com/api/v10/channels/{_DISCORD_CHANNEL}/messages"
_QUEUE_ID = "q-synthetic-slack"
_JOB_NAME = "synthetic_slack_job"
_TERMINAL_JOB_ID = "task_terminal_123"  # set by write_completed_run_state
_NOTIFICATION_THREADS = frozenset({"orca-queued-notification", "orca-terminal-notification"})
_PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def _slack_messenger(**extra: object) -> dict[str, object]:
    slack: dict[str, object] = {"bot_token": _SLACK_TOKEN, "default_channel_id": _SLACK_CHANNEL}
    slack.update(extra)
    return {"provider": "slack", "slack": slack}


@dataclass(frozen=True)
class _Reply:
    status: int
    reason: str
    body: Mapping[str, object]
    headers: tuple[tuple[str, str], ...] = ()


_SLACK_CONFIRMED = _Reply(
    200, "OK", {"ok": True, "channel": _SLACK_CHANNEL, "ts": "1700000000.000100"}
)
_SLACK_RATE_LIMITED = _Reply(
    429, "Too Many Requests", {"ok": False, "error": "ratelimited"}, (("Retry-After", "1"),)
)


@dataclass(frozen=True)
class _Outgoing:
    url: str
    method: str
    headers: dict[str, str]
    body: bytes | None
    thread: str


@dataclass
class _Transport:
    """Answers Slack from a script (the last reply repeats) and Discord with a confirmation."""

    slack_replies: list[_Reply] = field(default_factory=lambda: [_SLACK_CONFIRMED])
    outgoing: list[_Outgoing] = field(default_factory=list)
    streams: list[BytesIO] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def respond(self, request: urllib.request.Request) -> addinfourl:
        data = request.data
        with self.lock:
            self.outgoing.append(
                _Outgoing(
                    url=request.full_url,
                    method=request.get_method(),
                    headers=dict(request.header_items()),
                    body=data if isinstance(data, bytes) else None,
                    thread=threading.current_thread().name,
                )
            )
            if request.full_url == _SLACK_URL:
                replies = self.slack_replies
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
            elif request.full_url == _DISCORD_URL:
                reply = _Reply(200, "OK", {"id": "223456789012345678"})
            else:
                raise AssertionError("unexpected notification target")
            headers = EmailMessage()
            headers["Content-Type"] = "application/json; charset=utf-8"
            for name, value in reply.headers:
                headers[name] = value
            stream = BytesIO(json.dumps(reply.body).encode("utf-8"))
            self.streams.append(stream)
        response = addinfourl(stream, headers, request.full_url, reply.status)
        response.msg = reply.reason  # type: ignore[attr-defined]
        return response

    def urls(self) -> list[str]:
        with self.lock:
            return [request.url for request in self.outgoing]


def _join_notification_threads() -> None:
    for thread in threading.enumerate():
        if thread.name in _NOTIFICATION_THREADS:
            thread.join(timeout=10)
            assert not thread.is_alive(), f"{thread.name} did not finish"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Transport]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a queue notification test must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for variable in _PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)

    fake = _Transport()

    def https_open(_handler: object, request: urllib.request.Request) -> addinfourl:
        return fake.respond(request)

    def http_open(_handler: object, request: urllib.request.Request) -> addinfourl:
        return fake.respond(request)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    monkeypatch.setattr(time, "sleep", fake.sleeps.append)
    yield fake
    # Background senders must finish before the transport patches are undone.
    _join_notification_threads()


def _loaded(tmp_path: Path, messenger: Mapping[str, object]) -> tuple[AppConfig, Path]:
    cfg = load_config(str(_write_config(tmp_path, messenger)))
    # The real seams, not a test channel or a synchronous dispatcher.
    assert queue_notifications.notification_channel is orca_notifications.notification_channel
    assert queue_notifications.dispatch_notification is orca_notifications.dispatch_notification
    return cfg, Path(cfg.runtime.allowed_root)


def _save_queued_row(root: Path) -> None:
    entry = make_queue_entry(
        queue_id=_QUEUE_ID,
        reaction_dir=root / _JOB_NAME,
        metadata={QUEUED_NOTIFICATION_PENDING_KEY: True},
    )
    persistence.save_entries(root, [entry])


def _queued_claim_pending(root: Path) -> object:
    [row] = list_queue(root)
    return row.metadata.get(QUEUED_NOTIFICATION_PENDING_KEY)


def _slack_text(transport: _Transport) -> str:
    request = next(request for request in transport.outgoing if request.url == _SLACK_URL)
    assert request.body is not None
    payload = json.loads(request.body)
    assert payload["channel"] == _SLACK_CHANNEL
    return str(payload["text"])


# --------------------------------------------------------------------------- #
# Queued claim: durable before dispatch, one background Slack send, no replay
# --------------------------------------------------------------------------- #
def test_enabled_slack_queued_claim_is_durable_before_one_background_delivery(
    tmp_path: Path,
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg, root = _loaded(tmp_path, _slack_messenger())
    _save_queued_row(root)

    with caplog.at_level("DEBUG"):
        queue_notifications.notify_queued_jobs(cfg)
        # The claim was written before the sender was handed the event.
        assert _queued_claim_pending(root) is False
        claimed = persistence.queue_path(root).read_bytes()
        _join_notification_threads()

    assert transport.urls() == [_SLACK_URL]
    [request] = transport.outgoing
    assert request.thread == "orca-queued-notification"
    assert request.headers.get("Authorization") == f"Bearer {_SLACK_TOKEN}"
    text = _slack_text(transport)
    assert "ORCA queued" in text
    assert _QUEUE_ID in text
    assert transport.sleeps == []
    assert all(stream.closed for stream in transport.streams)
    # Delivery never writes the queue.
    assert persistence.queue_path(root).read_bytes() == claimed
    assert "queue_enqueued_notification_sent" in caplog.text
    assert _SLACK_TOKEN not in caplog.text

    # A reconstructed owner finds the claim and sends nothing more.
    queue_notifications.notify_queued_jobs(cfg)
    _join_notification_threads()
    assert transport.urls() == [_SLACK_URL]


@pytest.mark.parametrize(
    ("replies", "extra", "expected_requests", "expected_sleeps", "error"),
    [
        pytest.param(
            [_Reply(200, "OK", {"ok": True, "channel": "C0OTHER01", "ts": "1700000000.000100"})],
            {},
            1,
            [],
            "slack_invalid_response",
            id="channel-mismatch",
        ),
        pytest.param(
            [_Reply(200, "OK", {"ok": False, "error": "synthetic_failure"})],
            {},
            1,
            [],
            "slack_api_error",
            id="api-error",
        ),
        pytest.param(
            [_SLACK_RATE_LIMITED],
            {"max_attempts": 2},
            2,
            [1.0],
            "slack_http_429",
            id="rate-limited-bounded",
        ),
    ],
)
def test_failed_slack_delivery_keeps_the_queued_claim_and_is_not_replayed(
    tmp_path: Path,
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    replies: list[_Reply],
    extra: dict[str, object],
    expected_requests: int,
    expected_sleeps: list[float],
    error: str,
) -> None:
    transport.slack_replies = list(replies)
    cfg, root = _loaded(tmp_path, _slack_messenger(**extra))
    _save_queued_row(root)

    with caplog.at_level("DEBUG"):
        queue_notifications.notify_queued_jobs(cfg)
        claimed = persistence.queue_path(root).read_bytes()
        _join_notification_threads()

    assert transport.urls() == [_SLACK_URL] * expected_requests
    assert transport.sleeps == expected_sleeps
    assert all(stream.closed for stream in transport.streams)
    # A failed send is a missed advisory message: the claim stays, nothing is rewritten.
    assert _queued_claim_pending(root) is False
    assert persistence.queue_path(root).read_bytes() == claimed
    assert f"slack_bot_send_failed: {error}" in caplog.text
    assert "queue_enqueued_notification_failed" in caplog.text
    assert _SLACK_TOKEN not in caplog.text

    queue_notifications.notify_queued_jobs(cfg)
    _join_notification_threads()
    assert transport.urls() == [_SLACK_URL] * expected_requests


# --------------------------------------------------------------------------- #
# Terminal claim: durable in job_state before dispatch, delivery never writes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("replies", "sent_log"),
    [
        pytest.param([_SLACK_CONFIRMED], "run_finished_notification_sent", id="confirmed"),
        pytest.param([_SLACK_RATE_LIMITED], "run_finished_notification_failed", id="rate-limited"),
    ],
)
def test_enabled_slack_terminal_claim_is_durable_and_delivery_never_writes_state(
    tmp_path: Path,
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    replies: list[_Reply],
    sent_log: str,
) -> None:
    transport.slack_replies = list(replies)
    cfg, root = _loaded(tmp_path, _slack_messenger())
    job_dir = root / _JOB_NAME
    write_completed_run_state(job_dir)

    with caplog.at_level("DEBUG"):
        dispatched = queue_notifications.claim_and_send_terminal(
            cfg, str(job_dir), expected_job_id=_TERMINAL_JOB_ID
        )
        claimed = state_path(job_dir).read_bytes()
        _join_notification_threads()

    assert dispatched is True
    state = load_state(job_dir)
    assert state is not None
    final_result = state.get("final_result")
    assert final_result is not None
    assert final_result.get("finished_notification_claimed_at")
    assert not final_result.get("finished_notification_sent_at")
    # Delivery, confirmed or failed, never writes the run state.
    assert state_path(job_dir).read_bytes() == claimed
    assert transport.urls() == [_SLACK_URL]
    [request] = transport.outgoing
    assert request.thread == "orca-terminal-notification"
    text = _slack_text(transport)
    assert "ORCA completed" in text
    assert _JOB_NAME in text
    assert transport.sleeps == []
    assert sent_log in caplog.text
    assert _SLACK_TOKEN not in caplog.text

    # One claim: a reconstructed owner neither dispatches nor writes again.
    assert not queue_notifications.claim_and_send_terminal(
        cfg, str(job_dir), expected_job_id=_TERMINAL_JOB_ID
    )
    _join_notification_threads()
    assert state_path(job_dir).read_bytes() == claimed
    assert transport.urls() == [_SLACK_URL]


# --------------------------------------------------------------------------- #
# Controls: Discord on the same path; an incomplete Slack config claims nothing
# --------------------------------------------------------------------------- #
def test_existing_discord_terminal_claim_still_delivers_to_discord_only(
    tmp_path: Path,
    transport: _Transport,
) -> None:
    cfg, root = _loaded(
        tmp_path,
        {
            "provider": "discord",
            "discord": {"bot_token": _DISCORD_TOKEN, "default_channel_id": _DISCORD_CHANNEL},
        },
    )
    job_dir = root / _JOB_NAME
    write_completed_run_state(job_dir)

    assert queue_notifications.claim_and_send_terminal(
        cfg, str(job_dir), expected_job_id=_TERMINAL_JOB_ID
    )
    _join_notification_threads()

    assert transport.urls() == [_DISCORD_URL]
    [request] = transport.outgoing
    assert request.thread == "orca-terminal-notification"
    assert request.headers.get("Authorization") == f"Bot {_DISCORD_TOKEN}"


def test_incomplete_slack_config_leaves_both_claims_untouched(
    tmp_path: Path,
    transport: _Transport,
) -> None:
    cfg, root = _loaded(tmp_path, {"provider": "slack", "slack": {"bot_token": _SLACK_TOKEN}})
    _save_queued_row(root)
    job_dir = root / _JOB_NAME
    write_completed_run_state(job_dir)
    queue_before = persistence.queue_path(root).read_bytes()
    state_before = state_path(job_dir).read_bytes()

    queue_notifications.notify_queued_jobs(cfg)
    assert not queue_notifications.claim_and_send_terminal(
        cfg, str(job_dir), expected_job_id=_TERMINAL_JOB_ID
    )
    _join_notification_threads()

    # A disabled messenger consumes no claim, so enabling it later still notifies.
    assert _queued_claim_pending(root) is True
    assert persistence.queue_path(root).read_bytes() == queue_before
    assert state_path(job_dir).read_bytes() == state_before
    assert transport.outgoing == []
