"""Redirect safety of Slack delivery through the real urllib opener machinery.

Only the lowest transport is replaced: ``HTTPSHandler.https_open`` and
``HTTPHandler.http_open`` answer with standard-library ``addinfourl``
responses and record every outgoing request. urllib's own error processing and
redirect handling still run, so these tests see what the adapter would really
send. Sockets are refused, the cached global opener and proxy settings are
isolated, and ``time.sleep`` only records delays.

Not following redirects is ORCA's security policy for notification delivery,
not a Slack documentation guarantee.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from email.message import Message as EmailMessage
from io import BytesIO
from urllib.response import addinfourl

import pytest

from orca_auto.core.config import SlackConfig
from orca_auto.core.messaging import Message
from orca_auto.core.messaging.slack_bot import SlackBotChannel

# Obviously synthetic values: no real Slack workspace, token, channel or payload.
_TOKEN = "xoxb-synthetic-redirect-token"
_CHANNEL = "C0SYNTHETIC1"
_TS = "1700000000.000100"
_URL = "https://slack.com/api/chat.postMessage"
_TITLE = "ORCA completed synthetic-redirect-marker"
_REASON = "Synthetic Redirect Reason"
_CONFIRMED = json.dumps({"ok": True, "channel": _CHANNEL, "ts": _TS}).encode("utf-8")
_PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


@dataclass(frozen=True)
class _Reply:
    status: int
    reason: str
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _Outgoing:
    url: str
    method: str
    headers: dict[str, str]
    body: bytes | None


@dataclass
class _Transport:
    """Answers the Slack URL from a script and any other URL with a confirmed 200."""

    initial: list[_Reply] = field(default_factory=list)
    outgoing: list[_Outgoing] = field(default_factory=list)
    streams: list[BytesIO] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def respond(self, request: urllib.request.Request) -> addinfourl:
        data = request.data
        self.outgoing.append(
            _Outgoing(
                url=request.full_url,
                method=request.get_method(),
                headers=dict(request.header_items()),
                body=data if isinstance(data, bytes) else None,
            )
        )
        if request.full_url == _URL and self.initial:
            reply = self.initial.pop(0) if len(self.initial) > 1 else self.initial[0]
        else:
            # Whatever a followed redirect reaches would look like success.
            reply = _Reply(200, "OK", _CONFIRMED)
        headers = EmailMessage()
        headers["Content-Type"] = "application/json; charset=utf-8"
        for name, value in reply.headers:
            headers[name] = value
        stream = BytesIO(reply.body)
        self.streams.append(stream)
        response = addinfourl(stream, headers, request.full_url, reply.status)
        # urllib's own handlers set the reason the same way after getresponse().
        response.msg = reply.reason  # type: ignore[attr-defined]
        return response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _Transport:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a Slack test must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for variable in _PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    # A fresh default opener for this test; the cached one is restored afterwards.
    monkeypatch.setattr(urllib.request, "_opener", None)

    fake = _Transport()

    def https_open(_handler: object, request: urllib.request.Request) -> addinfourl:
        return fake.respond(request)

    def http_open(_handler: object, request: urllib.request.Request) -> addinfourl:
        return fake.respond(request)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    monkeypatch.setattr(time, "sleep", fake.sleeps.append)
    return fake


def _channel(**overrides: object) -> SlackBotChannel:
    values: dict[str, object] = {"bot_token": _TOKEN, "default_channel_id": _CHANNEL}
    values.update(overrides)
    return SlackBotChannel(SlackConfig(**values))  # type: ignore[arg-type]


def _redirect(status: int, location: str) -> _Reply:
    return _Reply(status, _REASON, b"synthetic redirect body", (("Location", location),))


_LOCATIONS = [
    pytest.param("https://slack.com/api/elsewhere", id="same-origin"),
    pytest.param("https://redirect-target.invalid/collect", id="cross-host"),
    pytest.param("http://slack.com/api/chat.postMessage", id="https-to-http"),
]


# --------------------------------------------------------------------------- #
# Redirect handling: 301, 302, and 303 redirects are refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [301, 302, 303])
@pytest.mark.parametrize("location", _LOCATIONS)
def test_followable_redirect_is_not_followed_and_never_forwards_the_token(
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    status: int,
    location: str,
) -> None:
    transport.initial = [_redirect(status, location)]

    with caplog.at_level("DEBUG"):
        result = _channel(max_attempts=10).send(Message(title=_TITLE))

    # Exactly the original request: nothing reaches the redirect target.
    assert [request.url for request in transport.outgoing] == [_URL]
    [initial] = transport.outgoing
    assert initial.method == "POST"
    assert initial.headers.get("Authorization") == f"Bearer {_TOKEN}"
    assert (result.sent, result.skipped, result.error) == (False, False, f"slack_http_{status}")
    assert transport.sleeps == []
    assert all(stream.closed for stream in transport.streams)
    assert _TOKEN not in caplog.text
    assert "synthetic-redirect-marker" not in caplog.text
    assert location not in caplog.text
    assert _REASON not in caplog.text


# --------------------------------------------------------------------------- #
# Confirmed delivery and non-followed redirect controls
# --------------------------------------------------------------------------- #
def test_confirmed_post_reaches_the_real_urllib_transport_once(transport: _Transport) -> None:
    transport.initial = [_Reply(200, "OK", _CONFIRMED)]

    result = _channel(timeout_seconds=3.0).send(Message(title=_TITLE))

    assert (result.sent, result.skipped, result.error) == (True, False, "")
    [initial] = transport.outgoing
    assert initial.url == _URL
    assert initial.method == "POST"
    assert initial.headers.get("Authorization") == f"Bearer {_TOKEN}"
    assert initial.body is not None
    assert json.loads(initial.body)["channel"] == _CHANNEL
    assert transport.sleeps == []
    assert all(stream.closed for stream in transport.streams)


@pytest.mark.parametrize("status", [307, 308])
def test_post_redirect_that_urllib_refuses_is_not_followed(
    transport: _Transport,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    location = "https://redirect-target.invalid/collect"
    transport.initial = [_redirect(status, location)]

    with caplog.at_level("DEBUG"):
        result = _channel(max_attempts=10).send(Message(title=_TITLE))

    assert [request.url for request in transport.outgoing] == [_URL]
    assert (result.sent, result.skipped, result.error) == (False, False, f"slack_http_{status}")
    assert transport.sleeps == []
    assert all(stream.closed for stream in transport.streams)
    assert _TOKEN not in caplog.text
    assert location not in caplog.text
    assert _REASON not in caplog.text


def test_rate_limit_retry_still_works_through_the_real_urllib_transport(
    transport: _Transport,
) -> None:
    transport.initial = [
        _Reply(429, "Too Many Requests", b'{"ok": false}', (("Retry-After", "1"),)),
        _Reply(200, "OK", _CONFIRMED),
    ]

    result = _channel(max_attempts=2).send(Message(title=_TITLE))

    assert (result.sent, result.error) == (True, "")
    assert [request.url for request in transport.outgoing] == [_URL, _URL]
    assert {request.method for request in transport.outgoing} == {"POST"}
    assert transport.outgoing[0].body == transport.outgoing[1].body
    assert transport.sleeps == [1.0]
    assert all(stream.closed for stream in transport.streams)
