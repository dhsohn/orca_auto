"""Resolve a :class:`MessageChannel` from a config file on disk.

The ``messenger:`` block selects the provider and owns all adapter settings.  A
legacy top-level ``telegram:`` block remains readable through the shared config
compatibility helper.
"""

from __future__ import annotations

import logging
from pathlib import Path

from orca_auto.core.config import MessengerConfig
from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    load_yaml_mapping,
    messenger_mapping_from_root,
)
from orca_auto.core.config.schema import messenger_config_from_mapping
from orca_auto.core.utils.coercion import normalize_text

from .channel import MessageChannel
from .registry import build_channel

_LOGGER = logging.getLogger(__name__)


def _load_messenger_config(config_path: str | Path | None) -> MessengerConfig:
    text = normalize_text(config_path)
    if not text:
        return MessengerConfig()
    try:
        path = Path(text).expanduser().resolve()
    except OSError:
        return MessengerConfig()
    if not path.exists():
        return MessengerConfig()
    try:
        _, raw = load_yaml_mapping(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        _LOGGER.warning(
            "failed to load messenger config file; notifications disabled: %s",
            path,
        )
        return MessengerConfig()
    return messenger_config_from_mapping(messenger_mapping_from_root(raw))


def load_messenger_config_from_file(config_path: str | Path | None) -> MessengerConfig:
    return _load_messenger_config(config_path)


def build_channel_from_config_path(
    config_path: str | Path | None,
    *,
    logger: logging.Logger | None = None,
) -> MessageChannel:
    """Load messenger config from ``config_path`` and resolve a channel."""
    return build_channel(_load_messenger_config(config_path), logger=logger)


__all__ = ["build_channel_from_config_path", "load_messenger_config_from_file"]
