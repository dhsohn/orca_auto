"""Resolve a :class:`MessageChannel` from a config file on disk.

Mirrors :func:`load_telegram_config_from_file`: the ``messenger:`` block selects
the provider and carries Discord settings, while Telegram credentials still come
from the top-level ``telegram:`` block (until Phase 1d folds them in).
"""

from __future__ import annotations

import logging
from pathlib import Path

from orca_auto.core.config import MessengerConfig, TelegramConfig
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS, load_yaml_mapping
from orca_auto.core.config.schema import (
    messenger_config_from_mapping,
    telegram_config_from_mapping,
)
from orca_auto.core.utils.coercion import normalize_text

from .channel import MessageChannel
from .registry import build_channel

_LOGGER = logging.getLogger(__name__)


def _load_config_mappings(config_path: str | Path | None) -> tuple[MessengerConfig, TelegramConfig]:
    text = normalize_text(config_path)
    if not text:
        return MessengerConfig(), TelegramConfig()
    try:
        path = Path(text).expanduser().resolve()
    except OSError:
        return MessengerConfig(), TelegramConfig()
    if not path.exists():
        return MessengerConfig(), TelegramConfig()
    try:
        _, raw = load_yaml_mapping(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        _LOGGER.debug("failed to load messenger config file: %s", path, exc_info=True)
        return MessengerConfig(), TelegramConfig()
    messenger_raw = raw.get("messenger")
    messenger_mapping = messenger_raw if isinstance(messenger_raw, dict) else {}
    return (
        messenger_config_from_mapping(messenger_mapping),
        telegram_config_from_mapping(messenger_mapping.get("telegram")),
    )


def load_messenger_config_from_file(config_path: str | Path | None) -> MessengerConfig:
    return _load_config_mappings(config_path)[0]


def build_channel_from_config_path(
    config_path: str | Path | None,
    *,
    logger: logging.Logger | None = None,
) -> MessageChannel:
    """Load messenger + telegram config from ``config_path`` and resolve a channel."""
    messenger, telegram = _load_config_mappings(config_path)
    return build_channel(messenger, telegram, logger=logger)


__all__ = ["build_channel_from_config_path", "load_messenger_config_from_file"]
