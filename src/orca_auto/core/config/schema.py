from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from orca_auto.core.ingest.policy import UploadPolicy, upload_policy_from_mapping
from orca_auto.core.utils.coercion import normalize_bool, normalize_text, safe_float, safe_int

_RuntimeAdmissionConfigT = TypeVar("_RuntimeAdmissionConfigT", bound="RuntimeAdmissionMixin")
_CONFIG_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CONFIG_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
SUPPORTED_MESSENGER_PROVIDERS = frozenset({"discord", "telegram"})
MIN_MESSENGER_TIMEOUT_SECONDS = 0.1
MAX_MESSENGER_TIMEOUT_SECONDS = 120.0
MAX_MESSENGER_ATTEMPTS = 10
MAX_MESSENGER_RETRY_BACKOFF_SECONDS = 120.0
_MAX_DISCORD_SNOWFLAKE = (1 << 64) - 1


def as_str(value: Any, default: str = "") -> str:
    return normalize_text(value, none=default)


def as_nonempty_str(value: Any, default: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value
    return default


def as_int(value: Any, default: int) -> int:
    return safe_int(value, default=default)


def as_bool(value: Any, default: bool = False) -> bool:
    return normalize_bool(
        value,
        default=default,
        true_values=_CONFIG_TRUE_VALUES,
        false_values=_CONFIG_FALSE_VALUES,
    )


def as_float(value: Any, default: float) -> float:
    parsed = safe_float(value, default=default)
    return default if parsed is None else parsed


def _bounded_delivery_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    parsed = as_float(value, default)
    if not math.isfinite(parsed):
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_delivery_attempts(value: Any, default: int) -> int:
    return min(MAX_MESSENGER_ATTEMPTS, max(1, as_int(value, default)))


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
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    return _discord_snowflake(value, field_name=field_name)


def _discord_snowflake_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a list of positive ASCII Discord snowflakes.")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        snowflake = _discord_snowflake(item, field_name=field_name)
        if snowflake not in seen:
            seen.add(snowflake)
            result.append(snowflake)
    return tuple(result)


def _telegram_user_id_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a list of positive ASCII Telegram user ids.")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        user_id = _positive_ascii_id(
            item,
            field_name=field_name,
            id_kind="Telegram user id",
        )
        if user_id not in seen:
            seen.add(user_id)
            result.append(user_id)
    return tuple(result)


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = safe_int(value, default=None)
    if parsed is None:
        return None
    return parsed if parsed > 0 else None


def explicit_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer >= 1.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} must be an integer >= 1.")
    parsed = safe_int(value, default=None)
    if parsed is None or parsed < 1:
        raise ValueError(f"{field_name} must be an integer >= 1.")
    return parsed


def normalize_default_max_retries(value: Any, default: int = 2) -> int:
    return max(0, as_int(value, default))


def normalize_max_concurrent(value: Any, default: int = 4) -> int:
    return max(1, as_int(value, default))


def normalize_admission_limit(value: Any, max_concurrent: int) -> int | None:
    del max_concurrent
    if value is None:
        return None
    if value == "":
        return None
    return explicit_positive_int(value, field_name="admission_limit")


def resolved_admission_limit(admission_limit: Any, max_concurrent: Any) -> int:
    fallback = normalize_max_concurrent(max_concurrent, 1)
    if admission_limit in (None, ""):
        return fallback
    return explicit_positive_int(admission_limit, field_name="admission_limit")


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
class CommonRuntimeConfig(RuntimeAdmissionMixin):
    allowed_root: str
    max_concurrent: int = 4
    admission_root: str | None = None
    admission_limit: int | None = None


@dataclass
class RetryRuntimeConfig(RuntimeAdmissionMixin):
    allowed_root: str = ""
    default_max_retries: int = 2
    max_concurrent: int = 4
    admission_root: str | None = ""
    admission_limit: int | None = None

    def __post_init__(self) -> None:
        self.default_max_retries = normalize_default_max_retries(
            self.default_max_retries,
            2,
        )
        self.max_concurrent = normalize_max_concurrent(
            self.max_concurrent,
            4,
        )
        if not self.admission_root and self.allowed_root:
            self.admission_root = self.allowed_root
        self.admission_limit = normalize_admission_limit(
            self.admission_limit,
            self.max_concurrent,
        )


@dataclass(frozen=True)
class CommonResourceConfig:
    max_cores_per_task: int = 8
    max_memory_gb_per_task: int = 32


@dataclass(frozen=True)
class EmptyBehaviorConfig:
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.5
    allowed_user_ids: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_user_ids",
            _telegram_user_id_tuple(
                self.allowed_user_ids,
                field_name="messenger.telegram.allowed_user_ids",
            ),
        )

    @property
    def enabled(self) -> bool:
        """Backward-compatible alias for outbound notification readiness."""

        return bool(str(self.bot_token).strip() and str(self.chat_id).strip())

    @property
    def interactive_enabled(self) -> bool:
        if not self.enabled:
            return False
        chat_id = str(self.chat_id).strip()
        numeric_chat = (
            chat_id.isascii() and chat_id.removeprefix("-").isdigit() and int(chat_id) != 0
        )
        if not numeric_chat:
            return False
        private_chat = not chat_id.startswith("-")
        return private_chat or bool(self.allowed_user_ids)


def telegram_config_from_mapping(raw: object) -> TelegramConfig:
    telegram_raw = raw if isinstance(raw, Mapping) else {}
    return TelegramConfig(
        bot_token=as_str(telegram_raw.get("bot_token")),
        chat_id=as_str(telegram_raw.get("chat_id")),
        timeout_seconds=_bounded_delivery_float(
            telegram_raw.get("timeout_seconds"),
            TelegramConfig.timeout_seconds,
            minimum=MIN_MESSENGER_TIMEOUT_SECONDS,
            maximum=MAX_MESSENGER_TIMEOUT_SECONDS,
        ),
        max_attempts=_bounded_delivery_attempts(
            telegram_raw.get("max_attempts"), TelegramConfig.max_attempts
        ),
        retry_backoff_seconds=_bounded_delivery_float(
            telegram_raw.get("retry_backoff_seconds"),
            TelegramConfig.retry_backoff_seconds,
            minimum=0.0,
            maximum=MAX_MESSENGER_RETRY_BACKOFF_SECONDS,
        ),
        allowed_user_ids=_telegram_user_id_tuple(
            telegram_raw.get("allowed_user_ids"),
            field_name="messenger.telegram.allowed_user_ids",
        ),
    )


@dataclass(frozen=True)
class DiscordConfig:
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.5
    bot_token: str = field(default="", repr=False)
    channel_ids: tuple[str, ...] = field(default=(), repr=False)
    default_channel_id: str = field(default="", repr=False)
    allowed_user_ids: tuple[str, ...] = field(default=(), repr=False)
    uploads: UploadPolicy = field(default_factory=UploadPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bot_token", self.bot_token.strip())
        object.__setattr__(
            self,
            "channel_ids",
            _discord_snowflake_tuple(
                self.channel_ids,
                field_name="messenger.discord.channel_ids",
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
        object.__setattr__(
            self,
            "allowed_user_ids",
            _discord_snowflake_tuple(
                self.allowed_user_ids,
                field_name="messenger.discord.allowed_user_ids",
            ),
        )

    @property
    def bot_notification_enabled(self) -> bool:
        return bool(self.bot_token.strip() and self.default_channel_id)

    @property
    def notification_enabled(self) -> bool:
        return self.bot_notification_enabled

    @property
    def interaction_channel_ids(self) -> tuple[str, ...]:
        if self.default_channel_id and self.default_channel_id not in self.channel_ids:
            return (*self.channel_ids, self.default_channel_id)
        return self.channel_ids

    @property
    def interactive_enabled(self) -> bool:
        return bool(self.bot_token.strip() and self.channel_ids and self.allowed_user_ids)

    @property
    def enabled(self) -> bool:
        """Backward-compatible alias for outbound notification readiness."""

        return self.notification_enabled


def discord_config_from_mapping(raw: object) -> DiscordConfig:
    discord_raw = raw if isinstance(raw, Mapping) else {}
    return DiscordConfig(
        bot_token=as_str(discord_raw.get("bot_token")),
        channel_ids=_discord_snowflake_tuple(
            discord_raw.get("channel_ids"),
            field_name="messenger.discord.channel_ids",
        ),
        default_channel_id=_optional_discord_snowflake(
            discord_raw.get("default_channel_id"),
            field_name="messenger.discord.default_channel_id",
        ),
        allowed_user_ids=_discord_snowflake_tuple(
            discord_raw.get("allowed_user_ids"),
            field_name="messenger.discord.allowed_user_ids",
        ),
        timeout_seconds=_bounded_delivery_float(
            discord_raw.get("timeout_seconds"),
            DiscordConfig.timeout_seconds,
            minimum=MIN_MESSENGER_TIMEOUT_SECONDS,
            maximum=MAX_MESSENGER_TIMEOUT_SECONDS,
        ),
        max_attempts=_bounded_delivery_attempts(
            discord_raw.get("max_attempts"), DiscordConfig.max_attempts
        ),
        retry_backoff_seconds=_bounded_delivery_float(
            discord_raw.get("retry_backoff_seconds"),
            DiscordConfig.retry_backoff_seconds,
            minimum=0.0,
            maximum=MAX_MESSENGER_RETRY_BACKOFF_SECONDS,
        ),
        uploads=upload_policy_from_mapping(discord_raw.get("uploads")),
    )


@dataclass(frozen=True)
class MessengerConfig:
    """Select the active outbound messenger and own all adapter configuration."""

    provider: str = "telegram"
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)

    @property
    def normalized_provider(self) -> str:
        return self.provider.strip().lower() or "telegram"

    @property
    def enabled(self) -> bool:
        if self.normalized_provider == "telegram":
            return self.telegram.enabled
        if self.normalized_provider == "discord":
            return self.discord.enabled
        return False


def messenger_config_from_mapping(raw: object) -> MessengerConfig:
    if raw is None:
        messenger_raw: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        messenger_raw = raw
    else:
        raise ValueError("messenger config must be a mapping when configured.")
    for adapter in ("telegram", "discord"):
        adapter_raw = messenger_raw.get(adapter)
        if adapter_raw is not None and not isinstance(adapter_raw, Mapping):
            raise ValueError(f"messenger.{adapter} must be a mapping when configured.")
    config = MessengerConfig(
        provider=as_str(messenger_raw.get("provider"), "telegram") or "telegram",
        telegram=telegram_config_from_mapping(messenger_raw.get("telegram")),
        discord=discord_config_from_mapping(messenger_raw.get("discord")),
    )
    if config.normalized_provider not in SUPPORTED_MESSENGER_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_MESSENGER_PROVIDERS))
        raise ValueError(
            f"Unsupported messenger.provider {config.provider!r}; expected one of: {supported}."
        )
    return config


def reconcile_legacy_telegram_alias(
    messenger: MessengerConfig,
    telegram: TelegramConfig,
) -> tuple[MessengerConfig, TelegramConfig]:
    """Keep programmatic ``AppConfig.telegram`` construction source-compatible.

    File loaders always construct both values from the canonical messenger block.
    For older callers that still pass only ``telegram=...``, promote that value
    into ``messenger.telegram``.  When both carry non-default values, the nested
    messenger-owned value wins.
    """
    default = TelegramConfig()
    if messenger.telegram == default and telegram != default:
        messenger = replace(messenger, telegram=telegram)
    else:
        telegram = messenger.telegram
    return messenger, telegram
