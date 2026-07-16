"""Shared notification transport and helper functions."""

from .telegram_api import TelegramApiClient
from .telegram_config import (
    DEFAULT_TELEGRAM_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    load_telegram_config_from_file,
)
from .telegram_format import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    escape_html,
    html_code,
    split_telegram_message,
)
from .telegram_transport import (
    TelegramDeliveryResult,
    TelegramSendResult,
    TelegramTransport,
    build_telegram_transport,
    log_telegram_send_failure,
    send_rendered_telegram_chunks,
    telegram_send_result_ok,
)

__all__ = [
    "DEFAULT_TELEGRAM_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TELEGRAM_MESSAGE_LENGTH",
    "TelegramApiClient",
    "TelegramDeliveryResult",
    "TelegramSendResult",
    "TelegramTransport",
    "build_telegram_transport",
    "escape_html",
    "html_code",
    "load_telegram_config_from_file",
    "log_telegram_send_failure",
    "send_rendered_telegram_chunks",
    "split_telegram_message",
    "telegram_send_result_ok",
]
