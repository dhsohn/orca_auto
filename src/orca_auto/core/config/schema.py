from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar
from urllib.parse import urlsplit

from orca_auto.core.utils.coercion import normalize_bool, normalize_text, safe_float, safe_int

_RuntimeAdmissionConfigT = TypeVar("_RuntimeAdmissionConfigT", bound="RuntimeAdmissionMixin")
_CONFIG_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CONFIG_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
SUPPORTED_MESSENGER_PROVIDERS = frozenset({"discord", "telegram"})
MIN_MESSENGER_TIMEOUT_SECONDS = 0.1
MAX_MESSENGER_TIMEOUT_SECONDS = 120.0
MAX_MESSENGER_ATTEMPTS = 10
MAX_MESSENGER_RETRY_BACKOFF_SECONDS = 120.0
_DISCORD_WEBHOOK_PATH = re.compile(r"^/api(?:/v\d+)?/webhooks/[1-9]\d{0,19}/[A-Za-z0-9._-]+$")
_DISCORD_WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "canary.discordapp.com",
        "ptb.discord.com",
        "ptb.discordapp.com",
    }
)


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


def discord_webhook_url_is_valid(raw_url: object) -> bool:
    """Accept only Discord's HTTPS execute-webhook endpoint shape."""

    if (
        not isinstance(raw_url, str)
        or raw_url != raw_url.strip()
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in raw_url)
    ):
        return False
    try:
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and host in _DISCORD_WEBHOOK_HOSTS
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and _DISCORD_WEBHOOK_PATH.fullmatch(parsed.path)
    )


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
    bot_token: str = ""
    chat_id: str = ""
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.5

    @property
    def enabled(self) -> bool:
        return bool(str(self.bot_token).strip() and str(self.chat_id).strip())


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
    )


@dataclass(frozen=True)
class DiscordConfig:
    webhook_url: str = ""
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.5

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url.strip())


def discord_config_from_mapping(raw: object) -> DiscordConfig:
    discord_raw = raw if isinstance(raw, Mapping) else {}
    config = DiscordConfig(
        webhook_url=as_str(discord_raw.get("webhook_url")),
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
    )
    if config.webhook_url and not discord_webhook_url_is_valid(config.webhook_url):
        raise ValueError(
            "messenger.discord.webhook_url must be an official HTTPS Discord webhook URL."
        )
    return config


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
