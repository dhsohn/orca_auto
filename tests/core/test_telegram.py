from __future__ import annotations

import json
from dataclasses import dataclass
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from orca_auto.core.config.schema import TelegramConfig
from orca_auto.core.notifications import telegram_api as telegram_api_mod
from orca_auto.core.notifications import telegram_config as telegram_config_mod
from orca_auto.core.notifications import telegram_format as telegram_format_mod
from orca_auto.core.notifications import telegram_network as telegram_network_mod
from orca_auto.core.notifications import telegram_transport as telegram_transport_mod


@dataclass
class _FakeResponse:
    body: str
    status: int

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        return False

    def read(self) -> bytes:
        return self.body.encode("utf-8")

    def getcode(self) -> int:
        return self.status


def _make_transport(
    *,
    bot_token: str = "bot-token",
    chat_id: str = "chat-id",
    timeout: float = 2.5,
    base_url: str = "https://example.test/",
) -> telegram_transport_mod.TelegramTransport:
    return telegram_transport_mod.TelegramTransport(
        config=TelegramConfig(bot_token=bot_token, chat_id=chat_id),
        timeout=timeout,
        base_url=base_url,
    )


def test_split_telegram_message_prefers_line_boundaries() -> None:
    chunks = telegram_format_mod.split_telegram_message("first\nsecond\nthird", limit=12)

    assert chunks == ["first", "second\nthird"]
    assert all(len(chunk) <= 12 for chunk in chunks)


def test_split_telegram_message_splits_long_segments() -> None:
    chunks = telegram_format_mod.split_telegram_message("abc def ghi", limit=7)

    assert chunks == ["abc def", "ghi"]
    assert all(len(chunk) <= 7 for chunk in chunks)


def test_disabled_transport_skips_without_calling_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("urlopen should not be called for disabled transport")

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport(bot_token="", chat_id="chat-id")

    result = transport.send_text("hello")

    assert result == telegram_transport_mod.TelegramSendResult(
        sent=False,
        skipped=True,
        error="telegram_disabled",
    )
    assert called is False


def test_empty_message_skips_without_calling_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("urlopen should not be called for empty messages")

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport()

    result = transport.send_text("   ")

    assert result == telegram_transport_mod.TelegramSendResult(
        sent=False,
        skipped=True,
        error="empty_message",
    )
    assert called is False


def test_incomplete_config_skips_without_calling_urlopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("urlopen should not be called for incomplete config")

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport()

    result = transport.send_text("hello", chat_id="   ")

    assert result == telegram_transport_mod.TelegramSendResult(
        sent=False,
        skipped=True,
        error="telegram_config_incomplete",
    )
    assert called is False


def test_successful_send_path_uses_urlopen_and_returns_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _FakeResponse(body='{"ok":true}', status=200)

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport(timeout=7.5, base_url="https://api.telegram.org/")

    result = transport.send_text(
        "  hello world  ",
        silent=True,
        parse_mode="MarkdownV2",
    )

    assert result.sent is True
    assert result.skipped is False
    assert result.status_code == 200
    assert result.response_text == '{"ok":true}'
    assert result.error == ""
    assert result.payload == {
        "chat_id": "chat-id",
        "text": "hello world",
        "disable_web_page_preview": "true",
        "disable_notification": "true",
        "parse_mode": "MarkdownV2",
    }

    request = seen["request"]
    assert request.full_url == "https://api.telegram.org/botbot-token/sendMessage"
    assert seen["timeout"] == 7.5
    assert request.data is not None
    parsed = parse_qs(request.data.decode("utf-8"))
    assert parsed == {
        "chat_id": ["chat-id"],
        "text": ["hello world"],
        "disable_web_page_preview": ["true"],
        "disable_notification": ["true"],
        "parse_mode": ["MarkdownV2"],
    }


def test_non_2xx_response_status_sets_http_error_and_preserves_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request, timeout):
        return _FakeResponse(body="service unavailable", status=503)

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport()

    result = transport.send_text("hello")

    assert result.sent is False
    assert result.skipped is False
    assert result.status_code == 503
    assert result.response_text == "service unavailable"
    assert result.error == "telegram_http_503"


def test_http_error_handling_reads_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout):
        raise HTTPError(
            url=request.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b"bad token"),
        )

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport()

    result = transport.send_text("hello")

    assert result.sent is False
    assert result.skipped is False
    assert result.status_code == 401
    assert result.response_text == "bad token"
    assert result.error == "telegram_http_error:HTTP Error 401: Unauthorized"


def test_log_telegram_send_failure_redacts_and_bounds_response_body() -> None:
    warnings: list[tuple[str, tuple[Any, ...]]] = []

    class FakeLogger:
        def warning(self, message: str, *args: Any) -> None:
            warnings.append((message, args))

    body = (
        json.dumps(
            {
                "ok": False,
                "description": "bad token 123456:abcdefghijklmnopqrstuvwxyz",
                "chat_id": "123456789",
                "text": "secret payload",
                "nested": {"message": "private message"},
            }
        )
        + "\n"
        + ("x" * 400)
    )

    telegram_transport_mod.log_telegram_send_failure(
        FakeLogger(),  # type: ignore[arg-type]
        telegram_transport_mod.TelegramSendResult(
            sent=False,
            status_code=400,
            error="telegram_http_400",
            response_text=body,
        ),
    )

    assert len(warnings) == 1
    assert warnings[0][0] == "telegram_send_failed: status=%s error=%s body=%s"
    logged_body = warnings[0][1][2]
    assert "123456:abcdefghijklmnopqrstuvwxyz" not in logged_body
    assert "123456789" not in logged_body
    assert "secret payload" not in logged_body
    assert "private message" not in logged_body
    assert "<redacted>" in logged_body
    assert "\n" not in logged_body
    assert len(logged_body) <= 303


def test_http_error_handling_leaves_response_text_empty_when_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenErrorBody:
        def read(self) -> bytes:
            raise OSError("cannot read body")

        def close(self) -> None:
            pass

    def fake_urlopen(request, timeout):
        raise HTTPError(
            url=request.full_url,
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=_BrokenErrorBody(),
        )

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport()

    result = transport.send_text("hello")

    assert result.sent is False
    assert result.skipped is False
    assert result.status_code == 502
    assert result.response_text == ""
    assert result.error == "telegram_http_error:HTTP Error 502: Bad Gateway"


def test_url_error_handling_returns_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout):
        raise URLError("temporary DNS failure")

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport()

    result = transport.send_text("hello")

    assert result.sent is False
    assert result.skipped is False
    assert result.status_code is None
    assert result.response_text == ""
    assert result.error == "telegram_url_error:<urlopen error temporary DNS failure>"


def test_network_unreachable_retries_with_ipv4_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise URLError(OSError(101, "Network is unreachable"))
        return _FakeResponse(body='{"ok":true}', status=200)

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport(base_url="https://api.telegram.org/")

    result = transport.send_text("hello")

    assert result.sent is True
    assert result.error == ""
    assert len(calls) == 2
    assert calls[0][0] == "https://api.telegram.org/botbot-token/sendMessage"
    assert calls[1][0] == "https://api.telegram.org/botbot-token/sendMessage"


def test_ipv4_fallback_resolver_override_is_hostname_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        (telegram_network_mod.socket.AF_INET6, None, None, None, ("::1", 443)),
        (telegram_network_mod.socket.AF_INET, None, None, None, ("127.0.0.1", 443)),
    ]

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return results

    monkeypatch.setattr(telegram_network_mod.socket, "getaddrinfo", fake_getaddrinfo)

    with telegram_network_mod._force_ipv4_resolution("api.telegram.org"):
        assert telegram_network_mod.socket.getaddrinfo("api.telegram.org", 443) == [results[1]]
        assert telegram_network_mod.socket.getaddrinfo("example.org", 443) == results

    assert telegram_network_mod.socket.getaddrinfo is fake_getaddrinfo


def test_build_telegram_transport_uses_defaults() -> None:
    config = TelegramConfig(bot_token="bot-token", chat_id="chat-id")

    transport = telegram_transport_mod.build_telegram_transport(config)

    assert transport.config is config
    assert transport.timeout == config.timeout_seconds
    assert transport.max_attempts == config.max_attempts
    assert transport.retry_backoff_seconds == config.retry_backoff_seconds
    assert transport.base_url == telegram_config_mod.DEFAULT_TELEGRAM_BASE_URL


def test_escape_helpers_and_config_loader(tmp_path: Path) -> None:
    assert telegram_format_mod.escape_html("<b>&test</b>") == "&lt;b&gt;&amp;test&lt;/b&gt;"
    assert telegram_format_mod.html_code("ready") == "<code>ready</code>"

    missing = telegram_config_mod.load_telegram_config_from_file(tmp_path / "missing.yaml")
    assert missing.enabled is False

    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                "telegram:",
                "  bot_token: bot-token",
                "  chat_id: chat-id",
                "  timeout_seconds: 7.5",
                "  max_attempts: 3",
                "  retry_backoff_seconds: 0.25",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = telegram_config_mod.load_telegram_config_from_file(config_path)
    assert config.bot_token == "bot-token"
    assert config.chat_id == "chat-id"
    assert config.timeout_seconds == 7.5
    assert config.max_attempts == 3
    assert config.retry_backoff_seconds == 0.25


def test_timeout_error_is_retried_and_can_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise URLError(TimeoutError("timed out"))
        return _FakeResponse(body='{"ok":true}', status=200)

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport(base_url="https://api.telegram.org/")

    result = transport.send_text("hello")

    assert result.sent is True
    assert len(calls) == 2


def test_retryable_http_error_is_retried_and_can_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise HTTPError(
                url=request.full_url,
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=BytesIO(b"busy"),
            )
        return _FakeResponse(body='{"ok":true}', status=200)

    monkeypatch.setattr(telegram_transport_mod, "urlopen", fake_urlopen)

    transport = _make_transport(base_url="https://api.telegram.org/")

    result = transport.send_text("hello")

    assert result.sent is True
    assert len(calls) == 2


def test_telegram_api_client_skips_empty_token_without_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        telegram_api_mod,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("transport should not run without a token"),
    )

    assert telegram_api_mod.TelegramApiClient(token="  ").api_call("getMe") is None


def test_telegram_api_client_posts_json_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_open(request, *, timeout: float):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["content_type"] = request.get_header("Content-type")
        seen["payload"] = json.loads((request.data or b"{}").decode("utf-8"))
        return _FakeResponse(body='{"ok": true, "result": {"message_id": 7}}', status=200)

    monkeypatch.setattr(telegram_api_mod, "urlopen", fake_open)

    client = telegram_api_mod.TelegramApiClient(
        token=" bot-token ",
        timeout=2.5,
        base_url="https://api.telegram.org/",
    )

    assert client.api_call("sendMessage", {"chat_id": 1, "text": "hello"}, timeout=8.0) == {
        "message_id": 7
    }
    assert seen == {
        "url": "https://api.telegram.org/botbot-token/sendMessage",
        "timeout": 8.0,
        "content_type": "application/json",
        "payload": {"chat_id": 1, "text": "hello"},
    }


def test_telegram_api_client_logs_non_ok_and_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, tuple[Any, ...]]] = []

    class FakeLogger:
        def warning(self, message: str, *args: Any) -> None:
            warnings.append((message, args))

    responses = iter(
        [
            _FakeResponse(
                body=json.dumps(
                    {
                        "ok": False,
                        "description": "chat not found",
                        "chat_id": "123456789",
                        "text": "private text",
                    }
                ),
                status=200,
            ),
            HTTPError(
                url="https://api.telegram.org/botbot-token/sendMessage",
                code=429,
                msg="Too Many Requests",
                hdrs=Message(),
                fp=BytesIO(
                    b'{"description":"retry later","token":"123456:abcdefghijklmnopqrstuvwxyz"}'
                ),
            ),
            RuntimeError("socket closed"),
        ]
    )

    def fake_open(request, *, timeout: float):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(telegram_api_mod, "urlopen", fake_open)
    client = telegram_api_mod.TelegramApiClient(token="bot-token", logger=FakeLogger())  # type: ignore[arg-type]

    assert client.api_call("sendMessage", {"text": "hello"}) is None
    assert client.api_call("sendMessage", {"text": "hello"}) is None
    assert client.api_call("sendMessage", {"text": "hello"}) is None

    assert [item[0] for item in warnings] == [
        "telegram_api_error: method=%s response=%s",
        "telegram_api_http_error: method=%s status=%d body=%s",
        "telegram_api_failed: method=%s error=%s",
    ]
    response_log = warnings[0][1][1]
    assert "123456789" not in response_log
    assert "private text" not in response_log
    assert "<redacted>" in response_log

    http_body_log = warnings[1][1][2]
    assert warnings[1][1][:2] == ("sendMessage", 429)
    assert "retry later" in http_body_log
    assert "123456:abcdefghijklmnopqrstuvwxyz" not in http_body_log
    assert "<redacted>" in http_body_log
