from __future__ import annotations

import socket
from typing import Any

import pytest

from orca_auto.core.config.schema import (
    DiscordConfig,
    as_nonempty_str,
    discord_config_from_mapping,
    messenger_config_from_mapping,
    positive_int_mapping,
)
from orca_auto.core.messaging import DisabledChannel, Message, build_channel
from orca_auto.core.messaging.discord_bot import DiscordBotChannel


def _refuse_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a messenger test must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_positive_int_mapping_keeps_only_positive_integer_values() -> None:
    assert positive_int_mapping("not a mapping") == {}
    assert positive_int_mapping(
        {"max_cores": "4", " ": 2, "max_memory_gb": 0, "flag": True, "bad": "x"}
    ) == {"max_cores": 4}


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (" /kept ", "fallback", " /kept "),
        ("   ", "fallback", "fallback"),
        (123, "fallback", "fallback"),
        (None, "fallback", "fallback"),
    ],
)
def test_as_nonempty_str_preserves_existing_string_behavior(
    value: object,
    default: str,
    expected: str,
) -> None:
    assert as_nonempty_str(value, default) == expected


def test_discord_delivery_settings_default_only_when_omitted_and_bound_finite_values() -> None:
    assert discord_config_from_mapping({}) == DiscordConfig()

    bounded = discord_config_from_mapping(
        {
            "timeout_seconds": 999,
            "max_attempts": 999,
            "retry_backoff_seconds": 999,
        }
    )
    assert bounded.timeout_seconds == 120.0
    assert bounded.max_attempts == 10
    assert bounded.retry_backoff_seconds == 120.0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("timeout_seconds", None, "must be a finite number"),
        ("timeout_seconds", True, "must be a finite number"),
        ("timeout_seconds", "", "must be a finite number"),
        ("timeout_seconds", "bad", "must be a finite number"),
        ("timeout_seconds", "nan", "must be a finite number"),
        ("timeout_seconds", "inf", "must be a finite number"),
        ("timeout_seconds", float("nan"), "must be a finite number"),
        ("timeout_seconds", float("inf"), "must be a finite number"),
        ("retry_backoff_seconds", None, "must be a finite number"),
        ("retry_backoff_seconds", False, "must be a finite number"),
        ("retry_backoff_seconds", "bad", "must be a finite number"),
        ("retry_backoff_seconds", float("-inf"), "must be a finite number"),
        ("max_attempts", None, "must be an integer"),
        ("max_attempts", True, "must be an integer"),
        ("max_attempts", "", "must be an integer"),
        ("max_attempts", "bad", "must be an integer"),
        ("max_attempts", "1.5", "must be an integer"),
        ("max_attempts", "nan", "must be an integer"),
        ("max_attempts", "inf", "must be an integer"),
        ("max_attempts", 1.5, "must be an integer"),
        ("max_attempts", float("nan"), "must be an integer"),
        ("max_attempts", float("inf"), "must be an integer"),
    ],
)
def test_discord_delivery_settings_reject_invalid_explicit_values(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=rf"messenger\..*\.{field} {message}"):
        discord_config_from_mapping({field: value})


def test_discord_config_parses_bot_notification_settings() -> None:
    config = discord_config_from_mapping(
        {
            "bot_token": " bot-token ",
            "default_channel_id": 333,
        }
    )

    assert config.bot_token == "bot-token"
    assert config.default_channel_id == "333"
    assert config.bot_notification_enabled


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            {"channe_ids": []},
            "Unknown messenger.discord config fields are not supported",
        ),
        (
            {"channel_ids": []},
            "Unknown messenger.discord config fields are not supported",
        ),
        (
            {"allowed_user_ids": []},
            "Unknown messenger.discord config fields are not supported",
        ),
    ],
)
def test_discord_config_rejects_unknown_fields(
    raw: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        discord_config_from_mapping(raw)


def test_messenger_unknown_field_error_does_not_echo_raw_key() -> None:
    secret_key = "private-secret-key"

    with pytest.raises(ValueError) as raised:
        messenger_config_from_mapping({secret_key: {}})

    assert secret_key not in str(raised.value)


@pytest.mark.parametrize(
    ("parser", "raw"),
    [
        (messenger_config_from_mapping, {"provider": "private-provider-value"}),
        (discord_config_from_mapping, {"default_channel_id": "private-bad-channel"}),
    ],
)
def test_messenger_config_validation_errors_do_not_echo_raw_values(
    parser: Any,
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValueError) as raised:
        parser(raw)

    assert "private-" not in str(raised.value)


def test_discord_bot_notification_fails_closed_on_incomplete_settings() -> None:
    token_only = DiscordConfig(bot_token="token")
    assert not token_only.bot_notification_enabled

    channel_only = DiscordConfig(default_channel_id="111")
    assert not channel_only.bot_notification_enabled

    complete = DiscordConfig(bot_token="token", default_channel_id="111")
    assert complete.bot_notification_enabled


def test_messenger_config_repr_redacts_credentials() -> None:
    discord = repr(
        DiscordConfig(
            bot_token="discord-secret",
            default_channel_id="789",
        )
    )

    assert "discord-secret" not in discord
    assert "789" not in discord


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_channel_id", "0"),
        ("default_channel_id", "001"),
        ("default_channel_id", "-1"),
        ("default_channel_id", "１２３"),
        ("default_channel_id", True),
        ("default_channel_id", str(1 << 64)),
    ],
)
def test_discord_config_rejects_invalid_snowflakes(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=rf"messenger\.discord\.{field}"):
        discord_config_from_mapping({field: value})


@pytest.mark.parametrize("value", [None, True, False, 123, 1.5, [], {}])
def test_discord_config_rejects_invalid_explicit_bot_tokens(value: object) -> None:
    with pytest.raises(ValueError, match="messenger.discord.bot_token must be a string"):
        discord_config_from_mapping({"bot_token": value})


@pytest.mark.parametrize(
    "value",
    [
        "synthetic-secret\ncontinuation",
        "synthetic-secret\rcontinuation",
        "synthetic-secret\tcontinuation",
        "synthetic secret",
        "synthetic-secret-\N{SNOWMAN}",
    ],
)
def test_discord_config_rejects_unsafe_bot_token_text_without_echoing(value: str) -> None:
    with pytest.raises(ValueError) as raised:
        discord_config_from_mapping({"bot_token": value})

    message = str(raised.value)
    assert "printable ASCII characters without whitespace" in message
    assert "synthetic-secret" not in message


def test_direct_discord_config_rejects_unsafe_bot_token_text() -> None:
    with pytest.raises(ValueError, match="printable ASCII characters without whitespace"):
        DiscordConfig(bot_token="synthetic-secret\ncontinuation")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_channel_id", None),
        ("default_channel_id", 1.5),
        ("default_channel_id", []),
        ("default_channel_id", {}),
    ],
)
def test_discord_config_rejects_invalid_explicit_identity_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=rf"messenger\.discord\.{field}"):
        discord_config_from_mapping({field: value})


def test_discord_config_preserves_empty_string_disable() -> None:
    config = discord_config_from_mapping(
        {
            "bot_token": "  ",
            "default_channel_id": "",
        }
    )

    assert config.bot_token == ""
    assert config.default_channel_id == ""
    assert not config.bot_notification_enabled


@pytest.mark.parametrize(
    "raw",
    [
        {"provider": "slack"},
        {"provider": " Slack "},
        {"provider": "slack", "slack": {}},
    ],
)
def test_slack_provider_without_slack_settings_is_accepted_and_cannot_send(
    raw: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_network(monkeypatch)

    config = messenger_config_from_mapping(raw)

    # Without Slack settings nothing is configured to deliver to.
    assert not config.enabled
    channel = build_channel(config)
    assert isinstance(channel, DisabledChannel)
    assert not channel.enabled
    result = channel.send(Message(title="ORCA queued"))
    assert (result.sent, result.skipped) == (False, True)


def test_discord_provider_and_unknown_providers_keep_their_behavior_alongside_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_network(monkeypatch)

    complete = messenger_config_from_mapping(
        {
            "provider": "discord",
            "discord": {"bot_token": "synthetic-token", "default_channel_id": "123"},
        }
    )
    assert complete.enabled
    # Built only; nothing is sent.
    assert isinstance(build_channel(complete), DiscordBotChannel)

    unset = messenger_config_from_mapping({"provider": "discord"})
    assert not unset.enabled
    assert isinstance(build_channel(unset), DisabledChannel)

    with pytest.raises(ValueError, match="Unsupported messenger.provider"):
        messenger_config_from_mapping({"provider": "telegram"})
