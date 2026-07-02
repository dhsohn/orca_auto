from __future__ import annotations

from typing import Any

from orca_auto.core.activity_icons import activity_status_icon

# Callback-query data tokens. Telegram caps callback_data at 64 bytes, so the
# prefixes are kept short and target ids are guarded against overflow.
_CB_CANCEL_DO = "cxl:y:"
_CB_CANCEL_NO = "cxl:n"
_CB_CANCEL_ASK = "cxl:a:"
_CB_REFRESH = "lst"
_CB_CLEAR = "lst:clr"
_CALLBACK_DATA_LIMIT = 64
_MAX_LIST_CANCEL_BUTTONS = 8
_LIST_BUTTON_LABEL_WIDTH = 30


def _inline_keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def _cancel_confirm_keyboard(target: str) -> dict[str, Any] | None:
    """Return a cancel confirmation keyboard when callback_data fits."""

    confirm_data = f"{_CB_CANCEL_DO}{target}"
    if len(confirm_data.encode("utf-8")) > _CALLBACK_DATA_LIMIT:
        return None
    return _inline_keyboard(
        [[_button("⛔ Yes, cancel", confirm_data), _button("✖ Keep", _CB_CANCEL_NO)]]
    )


def _list_button_label(item: dict[str, Any]) -> str:
    icon = activity_status_icon(str(item.get("status", "")))
    name = str(item.get("label") or item.get("activity_id") or "?").strip()
    if len(name) > _LIST_BUTTON_LABEL_WIDTH:
        name = name[: _LIST_BUTTON_LABEL_WIDTH - 1] + "…"
    return f"⛔ {icon} {name}"


def _list_action_keyboard(active_items: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for item in active_items[:_MAX_LIST_CANCEL_BUTTONS]:
        activity_id = str(item.get("activity_id") or "").strip()
        if not activity_id:
            continue
        data = f"{_CB_CANCEL_ASK}{activity_id}"
        if len(data.encode("utf-8")) > _CALLBACK_DATA_LIMIT:
            continue
        rows.append([_button(_list_button_label(item), data)])
    rows.append(
        [
            _button("🔄 Refresh", _CB_REFRESH),
            _button("🧹 Clear finished", _CB_CLEAR),
        ]
    )
    return _inline_keyboard(rows)
