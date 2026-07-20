from __future__ import annotations

from typing import Any, cast

import pytest

from orca_auto.core.config.schema import (
    CommonRuntimeConfig,
    DiscordConfig,
    RetryRuntimeConfig,
    TelegramConfig,
    as_bool,
    as_float,
    as_int,
    as_nonempty_str,
    as_str,
    discord_config_from_mapping,
    explicit_nonnegative_int,
    messenger_config_from_mapping,
    normalize_admission_limit,
    normalize_default_max_retries,
    normalize_max_concurrent,
    positive_int,
    telegram_config_from_mapping,
)


@pytest.mark.parametrize(
    ("allowed_root", "admission_root", "expected_root"),
    [
        ("/allowed", None, "/allowed"),
        ("/allowed", "/custom", "/custom"),
    ],
)
def test_common_runtime_config_resolved_admission_root(
    allowed_root: str,
    admission_root: str | None,
    expected_root: str,
) -> None:
    config = CommonRuntimeConfig(
        allowed_root=allowed_root,
        admission_root=admission_root,
    )

    assert config.resolved_admission_root == expected_root


@pytest.mark.parametrize(
    ("max_concurrent", "admission_limit", "expected_limit"),
    [
        (4, None, 4),
        (0, None, 1),
        (3, 2, 2),
    ],
)
def test_common_runtime_config_resolved_admission_limit_lower_bounds(
    max_concurrent: int,
    admission_limit: int | None,
    expected_limit: int,
) -> None:
    config = CommonRuntimeConfig(
        allowed_root="/allowed",
        max_concurrent=max_concurrent,
        admission_limit=admission_limit,
    )

    assert config.resolved_admission_limit == expected_limit


@pytest.mark.parametrize("admission_limit", [-7, 0, "bad", True])
def test_common_runtime_config_rejects_invalid_explicit_admission_limit(
    admission_limit: object,
) -> None:
    config = CommonRuntimeConfig(
        allowed_root="/allowed",
        max_concurrent=3,
        admission_limit=cast(Any, admission_limit),
    )

    with pytest.raises(ValueError, match="admission_limit must be an integer >= 1"):
        _ = config.resolved_admission_limit


def test_retry_runtime_config_normalizes_shared_runtime_fields() -> None:
    config = RetryRuntimeConfig(
        allowed_root="/runs/engine",
        default_max_retries=cast(Any, "-2"),
        max_concurrent=cast(Any, "0"),
        admission_limit=cast(Any, "2"),
    )

    assert config.default_max_retries == 0
    assert config.max_concurrent == 1
    assert config.admission_root == "/runs/engine"
    assert config.admission_limit == 2


@pytest.mark.parametrize("admission_limit", ["bad", "0", -1, True])
def test_retry_runtime_config_rejects_invalid_explicit_admission_limit(
    admission_limit: object,
) -> None:
    with pytest.raises(ValueError, match="admission_limit must be an integer >= 1"):
        RetryRuntimeConfig(
            allowed_root="/runs/engine",
            admission_limit=cast(Any, admission_limit),
        )


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


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, "fallback", "fallback"),
        ("  value  ", "fallback", "value"),
        (123, "", "123"),
    ],
)
def test_as_str_normalizes_config_text(value: object, default: str, expected: str) -> None:
    assert as_str(value, default) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("9", 2, 9),
        ("bad", 2, 2),
        (None, 2, 2),
    ],
)
def test_as_int_returns_config_default_for_invalid_values(
    value: object,
    default: int,
    expected: int,
) -> None:
    assert as_int(value, default) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, True, True),
        (" yes ", False, True),
        ("OFF", True, False),
        ("maybe", True, True),
    ],
)
def test_as_bool_uses_config_boolean_vocabulary(
    value: object,
    default: bool,
    expected: bool,
) -> None:
    assert as_bool(value, default) is expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("1.25", 2.0, 1.25),
        ("bad", 2.0, 2.0),
        (None, 2.0, 2.0),
    ],
)
def test_as_float_returns_config_default_for_invalid_values(
    value: object,
    default: float,
    expected: float,
) -> None:
    assert as_float(value, default) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7),
        ("0", None),
        ("bad", None),
        (True, None),
        (None, None),
    ],
)
def test_positive_int_accepts_only_positive_values(value: object, expected: int | None) -> None:
    assert positive_int(value) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("9", 2, 9),
        ("bad", 2, 2),
        ("-3", 2, 0),
    ],
)
def test_normalize_default_max_retries(value: object, default: int, expected: int) -> None:
    assert normalize_default_max_retries(value, default) == expected


@pytest.mark.parametrize(("value", "expected"), [(0, 0), ("7", 7), (8.0, 8)])
def test_explicit_nonnegative_int_accepts_integer_values(value: object, expected: int) -> None:
    assert explicit_nonnegative_int(value, field_name="retry") == expected


@pytest.mark.parametrize("value", [None, "", "bad", -1, True, 1.5])
def test_explicit_nonnegative_int_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match="retry must be an integer >= 0"):
        explicit_nonnegative_int(value, field_name="retry")


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("6", 4, 6),
        ("0", 4, 1),
        ("bad", 4, 4),
    ],
)
def test_normalize_max_concurrent(value: object, default: int, expected: int) -> None:
    assert normalize_max_concurrent(value, default) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2", 2),
    ],
)
def test_normalize_admission_limit(
    value: object,
    expected: int | None,
) -> None:
    assert normalize_admission_limit(value) == expected


@pytest.mark.parametrize("value", ["0", "bad", -1, True])
def test_normalize_admission_limit_rejects_invalid_explicit_values(value: object) -> None:
    with pytest.raises(ValueError, match="admission_limit must be an integer >= 1"):
        normalize_admission_limit(value)


@pytest.mark.parametrize(
    ("bot_token", "chat_id", "expected"),
    [
        ("", "", False),
        ("token", "", False),
        ("", "chat", False),
        ("   ", "chat", False),
        ("token", "   ", False),
        ("token", "chat", True),
    ],
)
def test_telegram_config_enabled(bot_token: str, chat_id: str, expected: bool) -> None:
    config = TelegramConfig(bot_token=bot_token, chat_id=chat_id)

    assert config.enabled is expected


def test_telegram_interactive_readiness_distinguishes_private_and_group_chats() -> None:
    assert TelegramConfig(bot_token="token", chat_id="123").interactive_enabled
    assert not TelegramConfig(bot_token="token", chat_id="-100123").interactive_enabled
    assert TelegramConfig(
        bot_token="token",
        chat_id="-100123",
        allowed_user_ids=("7",),
    ).interactive_enabled
    assert not TelegramConfig(
        bot_token="token",
        chat_id="@announcement",
        allowed_user_ids=("7",),
    ).interactive_enabled


def test_telegram_config_from_mapping_normalizes_delivery_settings() -> None:
    config = telegram_config_from_mapping(
        {
            "bot_token": " token ",
            "chat_id": 1234,
            "timeout_seconds": "0",
            "max_attempts": "0",
            "retry_backoff_seconds": "-2",
            "allowed_user_ids": [123, "456", "123"],
        }
    )

    assert config.bot_token == "token"
    assert config.chat_id == "1234"
    assert config.timeout_seconds == 0.1
    assert config.max_attempts == 1
    assert config.retry_backoff_seconds == 0.0
    assert config.allowed_user_ids == ("123", "456")


@pytest.mark.parametrize("value", [[False], ["0"], ["-1"], ["１２３"], "123,456"])
def test_telegram_config_rejects_invalid_allowed_user_ids(value: object) -> None:
    with pytest.raises(ValueError, match="messenger.telegram.allowed_user_ids"):
        telegram_config_from_mapping({"allowed_user_ids": value})


@pytest.mark.parametrize("value", [None, True, False, 123, 1.5, [], {}])
def test_telegram_config_rejects_invalid_explicit_bot_tokens(value: object) -> None:
    with pytest.raises(ValueError, match="messenger.telegram.bot_token must be a string"):
        telegram_config_from_mapping({"bot_token": value})


@pytest.mark.parametrize("value", [None, True, False, 1.5, [], {}])
def test_telegram_config_rejects_invalid_explicit_chat_ids(value: object) -> None:
    with pytest.raises(ValueError, match="messenger.telegram.chat_id must be a string or integer"):
        telegram_config_from_mapping({"chat_id": value})


@pytest.mark.parametrize("value", [None, True, 123, 1.5, {}, "123", [None], [1.5]])
def test_telegram_config_rejects_invalid_explicit_operator_lists(value: object) -> None:
    with pytest.raises(ValueError, match="messenger.telegram.allowed_user_ids"):
        telegram_config_from_mapping({"allowed_user_ids": value})


def test_telegram_config_preserves_empty_string_disable_and_empty_operator_list() -> None:
    config = telegram_config_from_mapping(
        {"bot_token": "  ", "chat_id": "", "allowed_user_ids": []}
    )

    assert config.bot_token == ""
    assert config.chat_id == ""
    assert config.allowed_user_ids == ()
    assert not config.enabled


def test_telegram_config_from_mapping_uses_defaults_for_non_mapping() -> None:
    assert telegram_config_from_mapping(None) == TelegramConfig()


@pytest.mark.parametrize(
    ("parser", "default"),
    [
        (telegram_config_from_mapping, TelegramConfig()),
        (discord_config_from_mapping, DiscordConfig()),
    ],
)
def test_messenger_delivery_settings_default_only_when_omitted_and_bound_finite_values(
    parser: Any,
    default: TelegramConfig | DiscordConfig,
) -> None:
    assert parser({}) == default

    bounded = parser(
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
    "parser",
    [telegram_config_from_mapping, discord_config_from_mapping],
)
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
def test_messenger_delivery_settings_reject_invalid_explicit_values(
    parser: Any,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=rf"messenger\..*\.{field} {message}"):
        parser({field: value})


def test_discord_config_parses_bot_and_authorization_settings() -> None:
    config = discord_config_from_mapping(
        {
            "bot_token": " bot-token ",
            "channel_ids": [111, "222", "111"],
            "default_channel_id": 333,
            "allowed_user_ids": ["444", 555, "444"],
        }
    )

    assert config.bot_token == "bot-token"
    assert config.channel_ids == ("111", "222")
    assert config.default_channel_id == "333"
    assert config.allowed_user_ids == ("444", "555")
    assert config.interaction_channel_ids == ("111", "222", "333")
    assert config.bot_notification_enabled
    assert config.interactive_enabled


def test_discord_config_uploads_default_disabled() -> None:
    config = discord_config_from_mapping({"bot_token": "t"})
    assert config.uploads.enabled is False


def test_discord_config_parses_upload_policy() -> None:
    config = discord_config_from_mapping(
        {
            "uploads": {
                "enabled": True,
                "max_archive_bytes": 1024,
                "max_entries": 10,
                "allowed_extensions": ["inp", ".XYZ", "yaml"],
            }
        }
    )

    assert config.uploads.enabled is True
    assert config.uploads.max_archive_bytes == 1024
    assert config.uploads.max_entries == 10
    # Extensions are normalized to a leading dot and lowercase.
    assert config.uploads.allowed_extensions == (".inp", ".xyz", ".yaml")


@pytest.mark.parametrize(
    ("parser", "raw", "message"),
    [
        (
            telegram_config_from_mapping,
            {"bot_tokn": "token"},
            "Unknown messenger.telegram config fields are not supported",
        ),
        (
            discord_config_from_mapping,
            {"channe_ids": []},
            "Unknown messenger.discord config fields are not supported",
        ),
        (
            discord_config_from_mapping,
            {"uploads": {"max_entry": 4}},
            "Unknown uploads config fields are not supported",
        ),
    ],
)
def test_messenger_adapter_config_rejects_unknown_fields(
    parser: Any,
    raw: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parser(raw)


@pytest.mark.parametrize(
    ("parser", "raw"),
    [
        (messenger_config_from_mapping, {"provider": "private-provider-value"}),
        (telegram_config_from_mapping, {"private-unknown-key": "value"}),
        (discord_config_from_mapping, {"uploads": {"max_entries": "private-limit-value"}}),
    ],
)
def test_messenger_config_validation_errors_do_not_echo_raw_values(
    parser: Any,
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValueError) as raised:
        parser(raw)

    assert "private-" not in str(raised.value)


@pytest.mark.parametrize("uploads", [None, "disabled", [], False])
def test_discord_config_rejects_non_mapping_uploads(uploads: object) -> None:
    with pytest.raises(ValueError, match="uploads must be a mapping"):
        discord_config_from_mapping({"uploads": uploads})


def test_discord_config_capabilities_are_independent_and_fail_closed() -> None:
    interactive = DiscordConfig(
        bot_token="token",
        channel_ids=("111",),
        allowed_user_ids=("222",),
    )
    assert interactive.interactive_enabled
    assert not interactive.bot_notification_enabled

    incomplete = DiscordConfig(bot_token="token")
    assert not incomplete.bot_notification_enabled
    assert not incomplete.interactive_enabled

    no_operators = DiscordConfig(bot_token="token", channel_ids=("111",))
    assert not no_operators.interactive_enabled

    default_only = DiscordConfig(
        bot_token="token",
        default_channel_id="111",
        allowed_user_ids=("222",),
    )
    assert default_only.bot_notification_enabled
    assert not default_only.interactive_enabled


def test_messenger_config_repr_redacts_credentials() -> None:
    telegram = repr(
        TelegramConfig(
            bot_token="telegram-secret",
            chat_id="123",
            allowed_user_ids=("7",),
        )
    )
    discord = repr(
        DiscordConfig(
            bot_token="discord-secret",
            channel_ids=("456",),
            default_channel_id="789",
            allowed_user_ids=("10",),
        )
    )

    assert "telegram-secret" not in telegram
    assert "123" not in telegram
    assert "('7',)" not in telegram
    assert "discord-secret" not in discord
    assert "('456',)" not in discord
    assert "789" not in discord
    assert "('10',)" not in discord


def test_discord_interaction_channels_include_default_once() -> None:
    config = DiscordConfig(
        bot_token="token",
        channel_ids=("111", "222"),
        default_channel_id="222",
    )

    assert config.interaction_channel_ids == ("111", "222")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_channel_id", "0"),
        ("default_channel_id", "001"),
        ("default_channel_id", "-1"),
        ("default_channel_id", "１２３"),
        ("default_channel_id", True),
        ("default_channel_id", str(1 << 64)),
        ("channel_ids", ["abc"]),
        ("channel_ids", "111,222"),
        ("allowed_user_ids", [False]),
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
    ("field", "value"),
    [
        ("default_channel_id", None),
        ("default_channel_id", 1.5),
        ("default_channel_id", []),
        ("default_channel_id", {}),
        ("channel_ids", None),
        ("channel_ids", True),
        ("channel_ids", 123),
        ("channel_ids", {}),
        ("allowed_user_ids", None),
        ("allowed_user_ids", False),
        ("allowed_user_ids", 123),
        ("allowed_user_ids", {}),
    ],
)
def test_discord_config_rejects_invalid_explicit_identity_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=rf"messenger\.discord\.{field}"):
        discord_config_from_mapping({field: value})


def test_discord_config_preserves_empty_string_disable_and_empty_allowlists() -> None:
    config = discord_config_from_mapping(
        {
            "bot_token": "  ",
            "channel_ids": [],
            "default_channel_id": "",
            "allowed_user_ids": [],
        }
    )

    assert config.bot_token == ""
    assert config.channel_ids == ()
    assert config.default_channel_id == ""
    assert config.allowed_user_ids == ()
    assert not config.interactive_enabled
