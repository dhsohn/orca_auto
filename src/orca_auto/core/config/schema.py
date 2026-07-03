from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from orca_auto.core.utils.coercion import normalize_bool, normalize_text, safe_float, safe_int

_RuntimeAdmissionConfigT = TypeVar("_RuntimeAdmissionConfigT", bound="RuntimeAdmissionMixin")
_CONFIG_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CONFIG_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


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
    organized_root: str
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
    organized_root: str
    max_concurrent: int = 4
    admission_root: str | None = None
    admission_limit: int | None = None


@dataclass
class RetryRuntimeConfig(RuntimeAdmissionMixin):
    allowed_root: str = ""
    organized_root: str = ""
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
        if not self.organized_root and self.allowed_root:
            self.organized_root = self.allowed_root
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
        return bool(self.bot_token and self.chat_id)


def telegram_config_from_mapping(raw: object) -> TelegramConfig:
    telegram_raw = raw if isinstance(raw, Mapping) else {}
    return TelegramConfig(
        bot_token=as_str(telegram_raw.get("bot_token")),
        chat_id=as_str(telegram_raw.get("chat_id")),
        timeout_seconds=max(
            0.1,
            as_float(telegram_raw.get("timeout_seconds"), TelegramConfig.timeout_seconds),
        ),
        max_attempts=max(1, as_int(telegram_raw.get("max_attempts"), TelegramConfig.max_attempts)),
        retry_backoff_seconds=max(
            0.0,
            as_float(
                telegram_raw.get("retry_backoff_seconds"),
                TelegramConfig.retry_backoff_seconds,
            ),
        ),
    )
