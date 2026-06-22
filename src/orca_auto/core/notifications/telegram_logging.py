from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_REDACTED = "<redacted>"
_SENSITIVE_KEYS = {
    "bot_token",
    "token",
    "access_token",
    "chat_id",
    "text",
    "message",
    "caption",
}
_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)([\"']?\b(?:bot_token|token|access_token|chat_id|text|message|caption)\b[\"']?\s*[:=]\s*)"
    r"([^,;&\n\r}\]]+)"
)


def _redact_text_patterns(text: str) -> str:
    text = _TELEGRAM_TOKEN_PATTERN.sub(_REDACTED, text)
    return _KEY_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}{_REDACTED}", text)


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_KEYS:
                redacted[key_text] = _REDACTED
            else:
                redacted[key_text] = _redact_json_value(item)
        return redacted
    if isinstance(value, str):
        return _redact_text_patterns(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact_json_value(item) for item in value]
    return value


def _maybe_redact_json_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return json.dumps(_redact_json_value(parsed), ensure_ascii=False, sort_keys=True)


def safe_log_body(body: Any, *, limit: int = 300) -> str:
    """Return a bounded Telegram API log fragment with secrets/message data redacted."""

    if isinstance(body, (Mapping, list, tuple)):
        text = json.dumps(_redact_json_value(body), ensure_ascii=False, sort_keys=True)
    else:
        raw_text = str(body)
        text = _maybe_redact_json_text(raw_text) or _redact_text_patterns(raw_text)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


__all__ = ["safe_log_body"]
