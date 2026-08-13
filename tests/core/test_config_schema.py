from __future__ import annotations

from typing import Any, cast

import pytest

from orca_auto.core.config.schema import (
    CommonRuntimeConfig,
    DiscordConfig,
    RetryRuntimeConfig,
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
    ("parser", "default"),
    [
        (discord_config_from_mapping, DiscordConfig()),
    ],
)
def test_messenger_delivery_settings_default_only_when_omitted_and_bound_finite_values(
    parser: Any,
    default: DiscordConfig,
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
    [discord_config_from_mapping],
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
    ("parser", "raw", "message"),
    [
        (
            discord_config_from_mapping,
            {"channe_ids": []},
            "Unknown messenger.discord config fields are not supported",
        ),
        (
            discord_config_from_mapping,
            {"channel_ids": []},
            "Unknown messenger.discord config fields are not supported",
        ),
        (
            discord_config_from_mapping,
            {"allowed_user_ids": []},
            "Unknown messenger.discord config fields are not supported",
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
