from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    load_yaml_mapping,
    messenger_mapping_from_root,
    validate_shared_config_sections,
)
from orca_auto.core.config.schema import TelegramConfig, telegram_config_from_mapping
from orca_auto.core.utils.coercion import normalize_text as _normalize_text

DEFAULT_TELEGRAM_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
LOGGER = logging.getLogger(__name__)


class TelegramConfigLike(Protocol):
    @property
    def bot_token(self) -> str: ...

    @property
    def chat_id(self) -> str: ...

    @property
    def timeout_seconds(self) -> float: ...

    @property
    def max_attempts(self) -> int: ...

    @property
    def retry_backoff_seconds(self) -> float: ...

    @property
    def enabled(self) -> bool: ...


def load_telegram_config_from_file(config_path: str | Path | None) -> TelegramConfig:
    config_text = _normalize_text(config_path)
    if not config_text:
        return TelegramConfig()

    try:
        path = Path(config_text).expanduser().resolve()
    except OSError:
        return TelegramConfig()
    if not path.exists():
        return TelegramConfig()

    try:
        _, raw = load_yaml_mapping(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        LOGGER.debug("failed to load telegram config file: %s", path, exc_info=True)
        return TelegramConfig()

    validate_shared_config_sections(raw)
    return telegram_config_from_mapping(messenger_mapping_from_root(raw).get("telegram"))


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "DEFAULT_TELEGRAM_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "LOGGER",
    "TelegramConfigLike",
    "load_telegram_config_from_file",
]
