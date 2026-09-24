from __future__ import annotations

import math
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from typing import Any

from orca_auto.core.utils.coercion import normalize_text, positive_int, safe_float, safe_int

# The only outbound messenger; ``messenger.provider`` stays accepted in YAML.
_MESSENGER_PROVIDER = "discord"
MIN_MESSENGER_TIMEOUT_SECONDS = 0.1
MAX_MESSENGER_TIMEOUT_SECONDS = 120.0
MAX_MESSENGER_ATTEMPTS = 10
MAX_MESSENGER_RETRY_BACKOFF_SECONDS = 120.0
_MAX_DISCORD_SNOWFLAKE = (1 << 64) - 1
_ASCII_INTEGER_PATTERN = re.compile(r"^[+-]?[0-9]+$")


def as_str(value: Any, default: str = "") -> str:
    return normalize_text(value, none=default)


def as_nonempty_str(value: Any, default: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value
    return default


def as_int(value: Any, default: int) -> int:
    return safe_int(value, default=default)


def _bounded_delivery_float(
    value: Any,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field_name} must be a finite number.")
    parsed = safe_float(value, default=None)
    if parsed is None or not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be a finite number.")
    return min(maximum, max(minimum, parsed))


def _bounded_delivery_attempts(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be an integer.")
        parsed = int(value)
    elif isinstance(value, str) and _ASCII_INTEGER_PATTERN.fullmatch(value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be an integer.")
    return min(MAX_MESSENGER_ATTEMPTS, max(1, parsed))


def _positive_ascii_id(
    value: object,
    *,
    field_name: str,
    id_kind: str,
    maximum: int = _MAX_DISCORD_SNOWFLAKE,
) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field_name} must be a positive ASCII {id_kind}.")
    text = str(value).strip()
    if (
        not text
        or not text.isascii()
        or not text.isdigit()
        or text.startswith("0")
        or len(text) > 20
        or int(text) > maximum
    ):
        raise ValueError(f"{field_name} must be a positive ASCII {id_kind}.")
    return text


def _discord_snowflake(value: object, *, field_name: str) -> str:
    """Normalize one Discord snowflake and reject ambiguous numeric text."""

    return _positive_ascii_id(value, field_name=field_name, id_kind="Discord snowflake")


def _optional_discord_snowflake(value: object, *, field_name: str) -> str:
    if isinstance(value, str) and not value.strip():
        return ""
    return _discord_snowflake(value, field_name=field_name)


def _optional_discord_bot_token(
    value: object,
    *,
    field_name: str,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when configured.")
    text = value.strip()
    if text and any(character < "!" or character > "~" for character in text):
        raise ValueError(
            f"{field_name} must contain only printable ASCII characters without whitespace."
        )
    return text


def explicit_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer >= 1.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} must be an integer >= 1.")
    parsed = safe_int(value, default=None)
    if parsed is None or parsed < 1:
        raise ValueError(f"{field_name} must be an integer >= 1.")
    return parsed


def resolved_admission_limit(admission_limit: Any, max_concurrent: Any) -> int:
    fallback = max(1, as_int(max_concurrent, 1))
    if admission_limit in (None, ""):
        return fallback
    return explicit_positive_int(admission_limit, field_name="admission_limit")


def positive_int_mapping(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        parsed = positive_int(value)
        if parsed is not None:
            result[key_text] = parsed
    return result


class RuntimeAdmissionMixin:
    allowed_root: str
    max_concurrent: int
    admission_root: str | None
    admission_limit: int | None

    @property
    def resolved_admission_root(self) -> str:
        return self.admission_root or self.allowed_root

    @property
    def resolved_admission_limit(self) -> int:
        return resolved_admission_limit(self.admission_limit, self.max_concurrent)


@dataclass(frozen=True)
class SchedulerConfig:
    """Validated top-level ``scheduler`` section.

    ``admission_root`` is the validated explicit path text, or "" when the
    section leaves it to the ``<runs_root>/.admission`` default. ``configured``
    records whether the operator wrote any scheduler key: only then is the
    admission limit pinned to ``max_active_simulations`` instead of following
    the worker's runtime concurrency.
    """

    max_active_simulations: int = 4
    admission_root: str = ""
    configured: bool = False

    @property
    def admission_limit(self) -> int | None:
        return self.max_active_simulations if self.configured else None


@dataclass(frozen=True)
class OrcaRuntimeConfig(RuntimeAdmissionMixin):
    """Validated ORCA runtime settings; ``load_config`` is the only producer.

    Frozen: a consumer that needs a different value (the queue worker's
    effective concurrency) derives a new instance with ``dataclasses.replace``.
    """

    allowed_root: str = ""
    max_concurrent: int = SchedulerConfig.max_active_simulations
    admission_root: str | None = None
    admission_limit: int | None = None


@dataclass(frozen=True)
class CommonResourceConfig:
    max_cores_per_task: int = 8
    max_memory_gb_per_task: int = 32


@dataclass(frozen=True)
class DiscordConfig:
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.5
    bot_token: str = field(default="", repr=False)
    default_channel_id: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bot_token",
            _optional_discord_bot_token(
                self.bot_token,
                field_name="messenger.discord.bot_token",
            ),
        )
        object.__setattr__(
            self,
            "default_channel_id",
            _optional_discord_snowflake(
                self.default_channel_id,
                field_name="messenger.discord.default_channel_id",
            ),
        )

    @property
    def bot_notification_enabled(self) -> bool:
        return bool(self.bot_token.strip() and self.default_channel_id)


def discord_config_from_mapping(raw: object) -> DiscordConfig:
    discord_raw = raw if isinstance(raw, Mapping) else {}
    reject_unknown_config_fields(
        discord_raw,
        allowed={
            "bot_token",
            "default_channel_id",
            "max_attempts",
            "retry_backoff_seconds",
            "timeout_seconds",
        },
        section="messenger.discord",
    )
    return DiscordConfig(
        bot_token=(
            _optional_discord_bot_token(
                discord_raw.get("bot_token"),
                field_name="messenger.discord.bot_token",
            )
            if "bot_token" in discord_raw
            else ""
        ),
        default_channel_id=(
            _optional_discord_snowflake(
                discord_raw.get("default_channel_id"),
                field_name="messenger.discord.default_channel_id",
            )
            if "default_channel_id" in discord_raw
            else ""
        ),
        timeout_seconds=(
            _bounded_delivery_float(
                discord_raw.get("timeout_seconds"),
                field_name="messenger.discord.timeout_seconds",
                minimum=MIN_MESSENGER_TIMEOUT_SECONDS,
                maximum=MAX_MESSENGER_TIMEOUT_SECONDS,
            )
            if "timeout_seconds" in discord_raw
            else DiscordConfig.timeout_seconds
        ),
        max_attempts=(
            _bounded_delivery_attempts(
                discord_raw.get("max_attempts"),
                field_name="messenger.discord.max_attempts",
            )
            if "max_attempts" in discord_raw
            else DiscordConfig.max_attempts
        ),
        retry_backoff_seconds=(
            _bounded_delivery_float(
                discord_raw.get("retry_backoff_seconds"),
                field_name="messenger.discord.retry_backoff_seconds",
                minimum=0.0,
                maximum=MAX_MESSENGER_RETRY_BACKOFF_SECONDS,
            )
            if "retry_backoff_seconds" in discord_raw
            else DiscordConfig.retry_backoff_seconds
        ),
    )


@dataclass(frozen=True)
class MessengerConfig:
    """Own the outbound messenger (Discord bot) configuration."""

    discord: DiscordConfig = field(default_factory=DiscordConfig)

    @property
    def enabled(self) -> bool:
        """Whether outbound notifications can be delivered."""
        return self.discord.bot_notification_enabled


def messenger_config_from_mapping(raw: object) -> MessengerConfig:
    if raw is None:
        messenger_raw: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        messenger_raw = raw
    else:
        raise ValueError("messenger config must be a mapping when configured.")
    reject_unknown_config_fields(
        messenger_raw,
        allowed={"discord", "provider"},
        section="messenger",
    )
    for adapter in ("discord",):
        adapter_raw = messenger_raw.get(adapter)
        if adapter in messenger_raw and not isinstance(adapter_raw, Mapping):
            raise ValueError(f"messenger.{adapter} must be a mapping when configured.")
    if "provider" in messenger_raw:
        provider = messenger_raw.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("messenger.provider must be a non-empty string when configured.")
        if provider.strip().lower() != _MESSENGER_PROVIDER:
            raise ValueError(f"Unsupported messenger.provider; expected {_MESSENGER_PROVIDER}.")
    return MessengerConfig(discord=discord_config_from_mapping(messenger_raw.get("discord")))


def reject_unknown_config_fields(
    raw: Mapping[Any, Any],
    *,
    allowed: Set[str],
    section: str,
) -> None:
    if any(key not in allowed for key in raw):
        # A malformed mapping key can itself be a misplaced credential. Keep
        # validation errors safe for CLI and journal output by naming only the
        # public section, never the raw key.
        raise ValueError(f"Unknown {section} config fields are not supported.")
