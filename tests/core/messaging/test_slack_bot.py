"""Focused tests for Slack ``chat.postMessage`` delivery through ``SlackBotChannel``.

No test opens a network connection: sockets are refused and every ``urlopen``
call is answered by a patched ``urllib.request.OpenerDirector.open``. Waits go
through a patched ``time.sleep`` that only records the delay.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.message import Message as EmailMessage
from http.client import IncompleteRead
from io import BytesIO
from typing import Literal, Self
from urllib.error import HTTPError, URLError

import pytest

from orca_auto.core.config import SlackConfig
from orca_auto.core.messaging import Message
from orca_auto.core.messaging.slack_bot import SlackBotChannel

# Obviously synthetic values: no real Slack workspace, token or channel.
_TOKEN = "xoxb-synthetic-test-token"
_CHANNEL = "C0SYNTHETIC1"
_OTHER_CHANNEL = "C0SYNTHETIC2"
_TS = "1700000000.000100"
_URL = "https://slack.com/api/chat.postMessage"
_TITLE = "ORCA completed synthetic-title-marker"


def _body(**fields: object) -> bytes:
    return json.dumps(fields).encode("utf-8")


_CONFIRMED = _body(ok=True, channel=_CHANNEL, ts=_TS)


class _FakeSlackResponse:
    headers = {"Content-Type": "application/json; charset=utf-8"}

    def __init__(self, body: bytes = _CONFIRMED, status: int = 200) -> None:
        self.status = status
        self._body = body

    def getcode(self) -> int:
        return self.status

    def read(self, *_args: object) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> Literal[False]:
        return False


def _http_error(code: int, *, retry_after: str | None = None, body: bytes = b"") -> HTTPError:
    headers = EmailMessage()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(_URL, code, "synthetic", headers, BytesIO(body))


@dataclass
class _Slack:
    """The patched opener: scripted answers, recorded requests and sleeps."""

    answers: list[_FakeSlackResponse | BaseException] = field(default_factory=list)
    requests: list[urllib.request.Request] = field(default_factory=list)
    timeouts: list[object] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def open(
        self,
        _opener: object,
        request: urllib.request.Request,
        data: object = None,
        timeout: object = None,
    ) -> _FakeSlackResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Slack]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a Slack test must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    fake = _Slack()

    def opener_open(
        opener: object,
        request: urllib.request.Request,
        data: object = None,
        timeout: object = None,
    ) -> _FakeSlackResponse:
        return fake.open(opener, request, data, timeout)

    # Every urlopen call, whichever module imported it, goes through an opener.
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", opener_open)
    monkeypatch.setattr(time, "sleep", fake.sleeps.append)
    yield fake


def _channel(**overrides: object) -> SlackBotChannel:
    values: dict[str, object] = {"bot_token": _TOKEN, "default_channel_id": _CHANNEL}
    values.update(overrides)
    return SlackBotChannel(SlackConfig(**values))  # type: ignore[arg-type]


def _assert_no_secrets_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert _TOKEN not in caplog.text
    assert "synthetic-title-marker" not in caplog.text


# --------------------------------------------------------------------------- #
# Delivery confirmation: channel ID and timestamp format
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_body(ok=True), id="no-channel-no-ts"),
        pytest.param(_body(ok=True, channel=_OTHER_CHANNEL, ts=_TS), id="other-channel"),
        pytest.param(_body(ok=True, channel=_CHANNEL), id="missing-ts"),
        pytest.param(_body(ok=True, channel=_CHANNEL, ts=""), id="empty-ts"),
        pytest.param(_body(ok=True, channel=_CHANNEL, ts="not-a-timestamp"), id="text-ts"),
        pytest.param(_body(ok=True, channel=_CHANNEL, ts=1700000000.0001), id="numeric-ts"),
        pytest.param(_body(ok=True, channel=_CHANNEL, ts="1700000000"), id="ts-without-fraction"),
    ],
)
def test_ok_true_without_the_configured_channel_and_a_timestamp_is_not_confirmed(
    slack: _Slack,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    slack.answers = [_FakeSlackResponse(body)]

    with caplog.at_level("DEBUG"):
        result = _channel().send(Message(title=_TITLE))

    assert (result.sent, result.skipped, result.error) == (False, False, "slack_invalid_response")
    assert len(slack.requests) == 1
    assert slack.sleeps == []
    _assert_no_secrets_logged(caplog)


# --------------------------------------------------------------------------- #
# Rate limiting: HTTP 429 Retry-After handling and bounds
# --------------------------------------------------------------------------- #
def test_rate_limited_send_waits_retry_after_once_then_delivers(slack: _Slack) -> None:
    rate_limit_body = BytesIO(b'{"ok": false, "error": "ratelimited"}')
    headers = EmailMessage()
    headers["Retry-After"] = "1"
    slack.answers = [
        HTTPError(_URL, 429, "synthetic", headers, rate_limit_body),
        _FakeSlackResponse(_CONFIRMED),
    ]

    result = _channel(max_attempts=2).send(Message(title=_TITLE))

    assert (result.sent, result.skipped, result.error) == (True, False, "")
    assert slack.sleeps == [1.0]
    assert len(slack.requests) == 2
    # The retry posts the identical body to the same fixed endpoint.
    assert slack.requests[0].data == slack.requests[1].data
    assert {request.full_url for request in slack.requests} == {_URL}
    assert rate_limit_body.closed


def test_rate_limit_retries_stop_at_ten_attempts(slack: _Slack) -> None:
    slack.answers = [_http_error(429, retry_after="1")]

    result = _channel(max_attempts=1_000_000).send(Message(title=_TITLE))

    assert not result.sent
    assert result.error == "slack_http_429"
    assert len(slack.requests) == 10
    assert slack.sleeps == [1.0] * 9


def test_rate_limit_waits_stay_within_the_120_second_budget(slack: _Slack) -> None:
    slack.answers = [_http_error(429, retry_after="50")]

    result = _channel(max_attempts=10).send(Message(title=_TITLE))

    assert not result.sent
    assert result.error in {"slack_http_429", "slack_retry_budget_exceeded"}
    # 50 s + 50 s fit the 120 s budget; a third 50 s wait would not.
    assert slack.sleeps == [50.0, 50.0]
    assert len(slack.requests) == 3


# --------------------------------------------------------------------------- #
# Delivery verification and error handling controls
# --------------------------------------------------------------------------- #
def test_matching_channel_and_canonical_timestamp_confirm_delivery(
    slack: _Slack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slack.answers = [_FakeSlackResponse(_CONFIRMED)]

    with caplog.at_level("DEBUG"):
        result = _channel(timeout_seconds=3.0).send(Message(title=_TITLE))

    assert (result.sent, result.skipped, result.error) == (True, False, "")
    [request] = slack.requests
    assert request.full_url == _URL
    assert request.get_header("Authorization") == f"Bearer {_TOKEN}"
    assert slack.timeouts == [3.0]
    assert slack.sleeps == []
    _assert_no_secrets_logged(caplog)


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("121", id="over-120-seconds"),
        pytest.param("1e9", id="huge"),
        pytest.param("inf", id="infinite"),
        pytest.param("nan", id="nan"),
        pytest.param("-1", id="negative"),
        pytest.param("true", id="bool-text"),
    ],
)
def test_unusable_retry_after_fails_closed_without_waiting(
    slack: _Slack,
    retry_after: str | None,
) -> None:
    error = _http_error(429, retry_after=retry_after, body=b'{"ok": false}')
    slack.answers = [error]

    result = _channel(max_attempts=2).send(Message(title=_TITLE))

    assert not result.sent
    assert result.error == "slack_http_429"
    assert len(slack.requests) == 1
    assert slack.sleeps == []
    assert error.fp is not None and error.fp.closed


@pytest.mark.parametrize(
    ("answer", "expected_error"),
    [
        pytest.param(URLError("synthetic connection reset"), "slack_network_error", id="url-error"),
        pytest.param(TimeoutError("synthetic timed out"), "slack_network_error", id="timeout"),
        pytest.param(_http_error(400, body=b'{"ok": false}'), "slack_http_400", id="http-400"),
        pytest.param(
            _FakeSlackResponse(_body(ok=False, error="synthetic_channel_not_found")),
            "slack_api_error",
            id="ok-false",
        ),
    ],
)
def test_ambiguous_or_permanent_failures_are_not_retried(
    slack: _Slack,
    caplog: pytest.LogCaptureFixture,
    answer: _FakeSlackResponse | BaseException,
    expected_error: str,
) -> None:
    slack.answers = [answer]

    with caplog.at_level("DEBUG"):
        result = _channel(max_attempts=10).send(Message(title=_TITLE))

    assert (result.sent, result.error) == (False, expected_error)
    # One attempt only: resending after an ambiguous failure could duplicate
    # the notification, and a permanent rejection will not change.
    assert len(slack.requests) == 1
    assert slack.sleeps == []
    assert "synthetic_channel_not_found" not in caplog.text
    assert "synthetic connection reset" not in caplog.text
    _assert_no_secrets_logged(caplog)
    if isinstance(answer, HTTPError):
        assert answer.fp is not None and answer.fp.closed


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not-json", id="not-json"),
        pytest.param(b'{"ok": true, "pad": "' + b"x" * 70_000 + b'"}', id="oversized"),
        pytest.param(b'{"ok": true, "channel": "\xff"}', id="invalid-utf8"),
    ],
)
def test_unreadable_success_bodies_report_invalid_response(
    slack: _Slack,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    slack.answers = [_FakeSlackResponse(body)]

    with caplog.at_level("DEBUG"):
        result = _channel(max_attempts=10).send(Message(title=_TITLE))

    assert (result.sent, result.error) == (False, "slack_invalid_response")
    assert len(slack.requests) == 1
    assert "x" * 100 not in caplog.text
    _assert_no_secrets_logged(caplog)


def test_truncated_success_body_is_a_network_error_without_retry(slack: _Slack) -> None:
    response = _FakeSlackResponse()

    def truncated(*_args: object) -> bytes:
        raise IncompleteRead(b'{"ok": true')

    response.read = truncated  # type: ignore[method-assign]
    slack.answers = [response]

    result = _channel(max_attempts=10).send(Message(title=_TITLE))

    assert (result.sent, result.error) == (False, "slack_network_error")
    assert len(slack.requests) == 1
    assert slack.sleeps == []


def test_request_errors_are_reported_without_the_token(
    slack: _Slack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slack.answers = [ValueError(f"Invalid header value b'Bearer {_TOKEN}'")]

    with caplog.at_level("DEBUG"):
        result = _channel(max_attempts=10).send(Message(title=_TITLE))

    assert (result.sent, result.error) == (False, "slack_request_error")
    assert len(slack.requests) == 1
    _assert_no_secrets_logged(caplog)


def test_unserializable_message_is_not_posted(
    slack: _Slack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slack.answers = [_FakeSlackResponse(_CONFIRMED)]

    with caplog.at_level("DEBUG"):
        result = _channel().send(Message(title="run \udcff finished"))

    assert (result.sent, result.error) == (False, "slack_request_error")
    assert slack.requests == []
    assert "\udcff" not in caplog.text
