from __future__ import annotations

from typing import Any, cast

import pytest

from orca_auto.core.config.schema import (
    CommonRuntimeConfig,
    RetryRuntimeConfig,
    TelegramConfig,
    as_bool,
    as_float,
    as_int,
    as_nonempty_str,
    as_str,
    normalize_admission_limit,
    normalize_default_max_retries,
    normalize_max_concurrent,
    positive_int,
    telegram_config_from_mapping,
)


@pytest.mark.parametrize(
    ("allowed_root", "organized_root", "admission_root", "expected_root"),
    [
        ("/allowed", "/organized", None, "/allowed"),
        ("/allowed", "/organized", "/custom", "/custom"),
    ],
)
def test_common_runtime_config_resolved_admission_root(
    allowed_root: str,
    organized_root: str,
    admission_root: str | None,
    expected_root: str,
) -> None:
    config = CommonRuntimeConfig(
        allowed_root=allowed_root,
        organized_root=organized_root,
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
        organized_root="/organized",
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
        organized_root="/organized",
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

    assert config.organized_root == "/runs/engine"
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
    ("value", "max_concurrent", "expected"),
    [
        (None, 4, None),
        ("2", 4, 2),
    ],
)
def test_normalize_admission_limit(
    value: object,
    max_concurrent: int,
    expected: int | None,
) -> None:
    assert normalize_admission_limit(value, max_concurrent) == expected


@pytest.mark.parametrize("value", ["0", "bad", -1, True])
def test_normalize_admission_limit_rejects_invalid_explicit_values(value: object) -> None:
    with pytest.raises(ValueError, match="admission_limit must be an integer >= 1"):
        normalize_admission_limit(value, 4)


@pytest.mark.parametrize(
    ("bot_token", "chat_id", "expected"),
    [
        ("", "", False),
        ("token", "", False),
        ("", "chat", False),
        ("token", "chat", True),
    ],
)
def test_telegram_config_enabled(bot_token: str, chat_id: str, expected: bool) -> None:
    config = TelegramConfig(bot_token=bot_token, chat_id=chat_id)

    assert config.enabled is expected


def test_telegram_config_from_mapping_normalizes_delivery_settings() -> None:
    config = telegram_config_from_mapping(
        {
            "bot_token": " token ",
            "chat_id": 1234,
            "timeout_seconds": "0",
            "max_attempts": "0",
            "retry_backoff_seconds": "-2",
        }
    )

    assert config.bot_token == "token"
    assert config.chat_id == "1234"
    assert config.timeout_seconds == 0.1
    assert config.max_attempts == 1
    assert config.retry_backoff_seconds == 0.0


def test_telegram_config_from_mapping_uses_defaults_for_non_mapping() -> None:
    assert telegram_config_from_mapping(None) == TelegramConfig()
