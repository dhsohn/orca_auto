from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.config import TelegramConfig
from orca_auto.core.notifications import telegram_api as telegram_api_mod
from orca_auto.core.notifications import telegram_format as telegram_format_mod
from orca_auto.flow.telegram import bot as bot
from tests.flow_factories import telegram_bot_settings


def _settings() -> bot.TelegramBotSettings:
    return telegram_bot_settings()


def _patch_send_transport(monkeypatch: pytest.MonkeyPatch, sender) -> None:
    class FakeTransport:
        def send_text(
            self,
            text: str,
            *,
            parse_mode: str | None = None,
            **kwargs: Any,
        ) -> SimpleNamespace:
            result = sender(text, parse_mode)
            if hasattr(result, "sent"):
                return result
            return SimpleNamespace(sent=bool(result))

    monkeypatch.setattr(bot, "build_telegram_transport", lambda _config: FakeTransport())


def test_handle_list_formats_unified_activity_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "list_activities",
        lambda **kwargs: {
            "activities": [
                {
                    "label": "wf-a",
                    "activity_id": "wf-a",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T01:00:00+00:00",
                    "metadata": {
                        "template_name": "reaction_ts_search",
                        "current_engine": "orca",
                        "request_parameters": {"crest_mode": "nci"},
                    },
                },
                {
                    "label": "mol-b",
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "running",
                    "source": "orca_auto_crest",
                    "submitted_at": "2026-04-26T01:10:00+00:00",
                    "updated_at": "2026-04-26T01:10:00+00:00",
                    "metadata": {
                        "task_kind": "conformer_search",
                        "job_dir": "/tmp/crest/wf-a/01_crest",
                    },
                },
                {
                    "label": "ts-1",
                    "activity_id": "orca-q-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "source": "orca_auto_orca",
                    "submitted_at": "2026-04-26T01:20:00+00:00",
                    "updated_at": "2026-04-26T01:20:00+00:00",
                    "metadata": {
                        "task_kind": "irc",
                        "reaction_dir": "/tmp/orca/runs/ts-1",
                    },
                },
            ]
        },
    )

    text = bot._handle_list(_settings(), "")

    assert "active_simulations: 2" in text
    assert "Status" in text and "Name" in text and "Detail" in text and "Elapsed" in text
    # The Telegram list omits the ID column, so activity ids are not rendered.
    assert "ID" not in text
    assert "orca-q-1" not in text
    assert "wf-a" in text
    assert "ts_search(nci)" in text
    # The crest child stays hidden (label ``mol-b``); the orca child shows by name.
    assert "mol-b" not in text
    assert "ts-1" in text
    assert "IRC" in text


def test_handle_list_filter_keeps_workflow_parent_for_visible_child(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "list_activities",
        lambda **kwargs: {
            "activities": [
                {
                    "label": "wf-a",
                    "activity_id": "wf-a",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T01:00:00+00:00",
                    "metadata": {
                        "template_name": "reaction_ts_search",
                        "current_engine": "crest",
                    },
                },
                {
                    "label": "mol-b",
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "pending",
                    "source": "orca_auto_crest",
                    "submitted_at": "2026-04-26T01:10:00+00:00",
                    "updated_at": "2026-04-26T01:10:00+00:00",
                    "metadata": {
                        "task_kind": "conformer_search",
                        "mode": "nci",
                        "job_dir": "/tmp/crest/wf-a/01_crest",
                    },
                },
            ]
        },
    )

    text = bot._handle_list(_settings(), "pending")

    assert "active_simulations: 0" in text
    assert "wf-a" in text
    assert "mol-b" in text
    assert "conformer_search(nci)" in text


def test_handle_list_uses_global_active_simulation_count_from_full_payload(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        bot,
        "list_activities",
        lambda **kwargs: {
            "activities": [
                {
                    "label": "hidden-run",
                    "activity_id": "orca-q-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "source": "orca_auto_orca",
                },
                {
                    "label": "visible-pending",
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "pending",
                    "source": "orca_auto_crest",
                },
            ],
            "sources": {"orca_config": "/tmp/orca_auto.yaml"},
        },
    )

    def _fake_count(items, *, config_path=None):
        captured["items"] = list(items)
        captured["config_path"] = config_path
        return 4

    monkeypatch.setattr(bot, "count_global_active_simulations", _fake_count)

    text = bot._handle_list(_settings(), "pending")

    assert "active_simulations: 4" in text
    assert len(captured["items"]) == 2
    assert captured["config_path"] == "/tmp/orca_auto.yaml"
    assert "visible-pending" in text
    assert "conformer_search" in text


def test_handle_list_shows_all_workflow_child_jobs(monkeypatch) -> None:
    child_rows = [
        {
            "label": f"ts-{index}",
            "activity_id": f"orca-q-{index}",
            "kind": "job",
            "engine": "orca",
            "status": "running",
            "source": "orca_auto_orca",
            "metadata": {
                "reaction_dir": f"/tmp/orca/wf-a/03_orca/case_{index:03d}",
            },
        }
        for index in range(1, 10)
    ]
    monkeypatch.setattr(
        bot,
        "list_activities",
        lambda **kwargs: {
            "activities": [
                {
                    "label": "wf-a",
                    "activity_id": "wf-a",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T01:00:00+00:00",
                    "metadata": {
                        "template_name": "reaction_ts_search",
                        "current_engine": "orca",
                    },
                },
                *child_rows,
            ]
        },
    )

    text = bot._handle_list(_settings(), "")

    assert "active_simulations: 9" in text
    # Activity ids are omitted; the nine orca children show by their ``ts-N`` names.
    assert "orca-q-" not in text
    assert text.count("ts-") == 9
    assert "wf-a" in text
    assert "ts_search" in text


def test_handle_list_clear_uses_shared_clear_activity_control(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "clear_activities",
        lambda **kwargs: {
            "total_cleared": 4,
            "cleared": {
                "workflows": 1,
                "xtb_queue_entries": 1,
                "crest_queue_entries": 0,
                "orca_queue_entries": 1,
                "orca_run_states": 1,
            },
        },
    )

    text = bot._handle_list(_settings(), "clear")

    assert "Cleared 4 completed/failed/cancelled entries." in text
    assert "workflows: 1" in text
    assert "xTB queue entries: 1" in text
    assert "ORCA queue entries: 1" in text
    assert "ORCA run states: 1" in text


def test_handle_list_reports_empty_activity_results(monkeypatch) -> None:
    monkeypatch.setattr(bot, "list_activities", lambda **kwargs: {"activities": []})
    monkeypatch.setattr(
        bot, "count_global_active_simulations", lambda items, *, config_path=None: 0
    )

    text = bot._handle_list(_settings(), "running")

    assert "active_simulations: 0" in text
    assert "No matching activities." in text


def test_activity_counter_config_path_falls_back_to_settings() -> None:
    settings = bot.TelegramBotSettings(
        telegram=TelegramConfig(bot_token="bot-token", chat_id="chat-id"),
        workflow_root=None,
        crest_config="",
        xtb_config="/tmp/xtb.yaml",
        orca_config=None,
        orca_repo_root=None,
    )

    assert bot._activity_counter_config_path({"sources": {}}, settings=settings) == "/tmp/xtb.yaml"
    assert (
        bot._activity_counter_config_path(
            {"sources": {"crest_config": " /tmp/crest.yaml "}},
            settings=settings,
        )
        == "/tmp/crest.yaml"
    )


def test_send_preformatted_response_wraps_chunks_in_pre(monkeypatch) -> None:
    sent: list[tuple[str, str | None]] = []

    def fake_send(text: str, parse_mode: str | None) -> bool:
        sent.append((text, parse_mode))
        return True

    _patch_send_transport(monkeypatch, fake_send)

    text = "\n".join(f"line-{index} {'x' * 20}" for index in range(8))

    assert bot._send_preformatted_response(_settings().telegram, text, limit=80)
    assert len(sent) > 1
    assert all(mode == "HTML" for _chunk, mode in sent)
    assert all(chunk.startswith("<pre>") and chunk.endswith("</pre>") for chunk, _mode in sent)


def test_split_telegram_message_rejects_non_positive_limit_and_splits_long_line() -> None:
    with pytest.raises(ValueError, match="positive"):
        telegram_format_mod.split_telegram_message("hello", limit=0)

    assert telegram_format_mod.split_telegram_message("abcdef", limit=2) == ["ab", "cd", "ef"]


def test_send_response_returns_false_when_all_send_attempts_fail(monkeypatch) -> None:
    _patch_send_transport(monkeypatch, lambda _text, _parse_mode: False)

    assert bot._send_response(_settings().telegram, "<b>hello</b>", parse_mode="HTML") is False


def test_send_preformatted_response_falls_back_to_plain_text_and_reports_failure(
    monkeypatch,
) -> None:
    sent_modes: list[str | None] = []

    def fake_send(text: str, parse_mode: str | None) -> SimpleNamespace:
        sent_modes.append(parse_mode)
        return SimpleNamespace(
            sent=parse_mode is None,
            status_code=400 if parse_mode else 200,
            response_text=(
                '{"ok":false,"description":"Bad Request: can\'t parse entities"}'
                if parse_mode
                else '{"ok":true}'
            ),
            error="telegram_http_400" if parse_mode else "",
        )

    _patch_send_transport(monkeypatch, fake_send)

    assert bot._send_preformatted_response(_settings().telegram, "hello")
    assert sent_modes == ["HTML", None]

    _patch_send_transport(monkeypatch, lambda _text, _parse_mode: False)
    assert bot._send_preformatted_response(_settings().telegram, "hello") is False

    with pytest.raises(ValueError, match="exceed wrapper"):
        bot._send_preformatted_response(_settings().telegram, "hello", limit=10)


def test_handle_cancel_routes_through_activity_control(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "cancel_activity",
        lambda **kwargs: {
            "label": "wf-a",
            "activity_id": "wf-a",
            "status": "cancel_requested",
        },
    )

    text = bot._handle_cancel(_settings(), "wf-a")

    assert "wf-a" in text
    assert "cancel_requested" in text


def test_handle_cancel_usage_and_error_paths(monkeypatch) -> None:
    assert "Usage:" in bot._handle_cancel(_settings(), "")

    monkeypatch.setattr(
        bot,
        "cancel_activity",
        lambda **kwargs: (_ for _ in ()).throw(LookupError("missing <target>")),
    )

    assert "missing &lt;target&gt;" in bot._handle_cancel(_settings(), "wf-missing")


def test_handle_help_mentions_only_supported_commands() -> None:
    text = bot._handle_help(_settings(), "")

    assert "/list" in text
    assert "/list clear" in text
    assert "/cancel" in text
    assert "/help" in text
    assert "/cron" not in text


def test_cancel_confirm_keyboard_structure_and_overflow_guard() -> None:
    keyboard = bot._cancel_confirm_keyboard("wf-a")
    assert keyboard is not None
    row = keyboard["inline_keyboard"][0]
    assert row[0]["callback_data"] == "cxl:y:wf-a"
    assert row[1]["callback_data"] == "cxl:n"

    # An over-long identifier cannot fit Telegram's 64-byte callback budget.
    assert bot._cancel_confirm_keyboard("x" * 80) is None


def test_send_cancel_confirmation_paths(monkeypatch) -> None:
    sends: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        bot,
        "_send_message",
        lambda settings, text, *, reply_markup=None, parse_mode="HTML": sends.append(
            (text, reply_markup)
        ),
    )
    plain: list[str] = []

    def _fake_send_response(config, text, **kwargs):
        plain.append(text)
        return True

    monkeypatch.setattr(bot, "_send_response", _fake_send_response)

    bot._send_cancel_confirmation(_settings(), "")
    assert "Usage:" in plain[0]

    bot._send_cancel_confirmation(_settings(), "wf-a")
    assert "Cancel" in sends[0][0]
    assert sends[0][1]["inline_keyboard"][0][0]["callback_data"] == "cxl:y:wf-a"

    # Over-long target falls back to a direct cancel via _handle_cancel.
    monkeypatch.setattr(bot, "_handle_cancel", lambda settings, target: f"cancelled {target[:3]}")
    bot._send_cancel_confirmation(_settings(), "y" * 80)
    assert plain[-1].startswith("cancelled yyy")


def test_callback_response_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(bot, "_handle_cancel", lambda settings, target: f"did {target}")

    assert bot._callback_response(_settings(), "cxl:n") == "✖ Cancellation dismissed."
    assert bot._callback_response(_settings(), "cxl:y:wf-a") == "did wf-a"
    assert bot._callback_response(_settings(), "bogus") == "Unknown action."


def test_dispatch_callback_query_answers_and_edits(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_api_call(token, method, payload=None, **kwargs):
        calls.append((method, payload or {}))
        return None

    monkeypatch.setattr(bot, "_api_call", fake_api_call)
    monkeypatch.setattr(bot, "_handle_cancel", lambda settings, target: f"done {target}")
    refreshed: dict[str, Any] = {}
    monkeypatch.setattr(
        bot, "_send_list_response", lambda settings: refreshed.setdefault("done", True)
    )

    update = {
        "update_id": 7,
        "callback_query": {
            "id": "cb-1",
            "data": "cxl:y:wf-a",
            "message": {"message_id": 99, "chat": {"id": "chat-id"}},
        },
    }

    assert bot._dispatch_callback_query(_settings(), update) == 7
    methods = [method for method, _payload in calls]
    assert "answerCallbackQuery" in methods
    edit = next(payload for method, payload in calls if method == "editMessageText")
    assert edit["message_id"] == 99
    assert edit["text"] == "done wf-a"
    # Executing a cancel refreshes the list so the actions reflect new state.
    assert refreshed.get("done") is True


def test_list_action_keyboard_builds_cancel_and_refresh_buttons() -> None:
    items = [
        {"activity_id": "wf-a", "label": "wf-a", "status": "running"},
        {"activity_id": "z" * 80, "label": "too-long", "status": "running"},
    ]
    keyboard = bot._list_action_keyboard(items)
    rows = keyboard["inline_keyboard"]

    assert rows[0][0]["callback_data"] == "cxl:a:wf-a"
    # The over-long id is skipped; only the wf-a cancel and the refresh remain.
    assert len(rows) == 2
    assert rows[-1][0]["callback_data"] == "lst"


def test_list_action_keyboard_caps_cancel_buttons() -> None:
    items = [{"activity_id": f"wf-{index}", "status": "running"} for index in range(20)]
    rows = bot._list_action_keyboard(items)["inline_keyboard"]
    # 8 cancel buttons (cap) + 1 refresh row.
    assert len(rows) == bot._MAX_LIST_CANCEL_BUTTONS + 1
    assert rows[-1][0]["callback_data"] == "lst"


def test_active_cancel_targets_filters_to_active(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "list_activities",
        lambda **kwargs: {
            "activities": [
                {
                    "activity_id": "wf-run",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                },
                {
                    "activity_id": "wf-done",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "completed",
                },
                {
                    "activity_id": "wf-pend",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "pending",
                },
            ]
        },
    )

    active = bot._active_cancel_targets(_settings())

    assert {item["activity_id"] for item in active} == {"wf-run", "wf-pend"}


def test_send_list_actions_sends_keyboard_and_is_exception_safe(monkeypatch) -> None:
    sent: list[tuple[str, Any]] = []

    def _fake_send_message(settings, text, *, reply_markup=None, parse_mode="HTML"):
        sent.append((text, reply_markup))

    monkeypatch.setattr(bot, "_send_message", _fake_send_message)
    monkeypatch.setattr(
        bot,
        "_active_cancel_targets",
        lambda settings: [{"activity_id": "wf-a", "label": "wf-a", "status": "running"}],
    )

    bot._send_list_actions(_settings())

    assert sent[0][0] == "🔧 Actions:"
    assert sent[0][1]["inline_keyboard"][0][0]["callback_data"] == "cxl:a:wf-a"

    def _boom(settings):
        raise RuntimeError("activity source down")

    monkeypatch.setattr(bot, "_active_cancel_targets", _boom)
    # Must not raise even when the activity source fails.
    bot._send_list_actions(_settings())


def test_send_list_actions_announces_cap_when_truncated(monkeypatch) -> None:
    sent: list[tuple[str, Any]] = []

    def _fake_send_message(settings, text, *, reply_markup=None, parse_mode="HTML"):
        sent.append((text, reply_markup))

    monkeypatch.setattr(bot, "_send_message", _fake_send_message)
    items = [{"activity_id": f"wf-{index}", "status": "running"} for index in range(12)]
    monkeypatch.setattr(bot, "_active_cancel_targets", lambda settings: items)

    bot._send_list_actions(_settings())

    assert f"showing {bot._MAX_LIST_CANCEL_BUTTONS} of 12" in sent[0][0]


def test_dispatch_callback_query_dismiss_does_not_refresh(monkeypatch) -> None:
    refreshed: dict[str, Any] = {}
    monkeypatch.setattr(bot, "_api_call", lambda *a, **k: None)
    monkeypatch.setattr(
        bot, "_send_list_response", lambda settings: refreshed.setdefault("done", True)
    )

    update = {
        "update_id": 11,
        "callback_query": {
            "id": "cb-5",
            "data": "cxl:n",
            "message": {"message_id": 2, "chat": {"id": "chat-id"}},
        },
    }

    assert bot._dispatch_callback_query(_settings(), update) == 11
    # Dismissing must not refresh the list.
    assert "done" not in refreshed


def test_dispatch_callback_query_refresh_resends_list(monkeypatch) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(bot, "_api_call", lambda *a, **k: None)
    monkeypatch.setattr(
        bot, "_send_list_response", lambda settings: calls.setdefault("refresh", True)
    )

    update = {
        "update_id": 9,
        "callback_query": {
            "id": "cb-3",
            "data": "lst",
            "message": {"message_id": 1, "chat": {"id": "chat-id"}},
        },
    }

    assert bot._dispatch_callback_query(_settings(), update) == 9
    assert calls.get("refresh") is True


def test_dispatch_callback_query_clear_prunes_and_refreshes(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bot, "_api_call", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_clear_finished", lambda settings: calls.append("clear"))
    monkeypatch.setattr(bot, "_send_list_response", lambda settings: calls.append("refresh"))

    update = {
        "update_id": 13,
        "callback_query": {
            "id": "cb-clr",
            "data": "lst:clr",
            "message": {"message_id": 1, "chat": {"id": "chat-id"}},
        },
    }

    assert bot._dispatch_callback_query(_settings(), update) == 13
    # Clearing prunes finished entries first, then refreshes the list.
    assert calls == ["clear", "refresh"]


def test_clear_finished_sends_clear_summary(monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(bot, "_send_response", lambda telegram, text: sent.append(text))
    monkeypatch.setattr(
        bot,
        "clear_activities",
        lambda **kwargs: {"total_cleared": 2, "cleared": {"workflows": 2}},
    )

    bot._clear_finished(_settings())

    assert sent and "Cleared 2" in sent[0]


def test_list_action_keyboard_includes_clear_button() -> None:
    items = [{"activity_id": "wf-a", "label": "wf-a", "status": "running"}]
    rows = bot._list_action_keyboard(items)["inline_keyboard"]
    last_row = rows[-1]
    callbacks = [button["callback_data"] for button in last_row]
    assert "lst" in callbacks
    assert "lst:clr" in callbacks


def test_dispatch_callback_query_ask_shows_confirmation(monkeypatch) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(bot, "_api_call", lambda *a, **k: None)
    monkeypatch.setattr(
        bot, "_send_cancel_confirmation", lambda settings, target: calls.setdefault("ask", target)
    )

    update = {
        "update_id": 10,
        "callback_query": {
            "id": "cb-4",
            "data": "cxl:a:wf-a",
            "message": {"message_id": 1, "chat": {"id": "chat-id"}},
        },
    }

    assert bot._dispatch_callback_query(_settings(), update) == 10
    assert calls.get("ask") == "wf-a"


def test_dispatch_callback_query_ignores_other_chats(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        bot, "_api_call", lambda token, method, payload=None, **kwargs: calls.append(method)
    )

    update = {
        "update_id": 8,
        "callback_query": {
            "id": "cb-2",
            "data": "cxl:y:wf-a",
            "message": {"message_id": 5, "chat": {"id": "intruder"}},
        },
    }

    assert bot._dispatch_callback_query(_settings(), update) == 8
    # Only the callback is acknowledged; no edit/cancel is performed.
    assert "editMessageText" not in calls


def test_send_response_splits_long_messages(monkeypatch) -> None:
    sent: list[tuple[str, str | None]] = []

    def fake_send(text: str, parse_mode: str | None) -> bool:
        sent.append((text, parse_mode))
        return True

    _patch_send_transport(monkeypatch, fake_send)

    text = "\n".join(f"<code>line-{index}</code> {'x' * 28}" for index in range(8))

    assert bot._send_response(_settings().telegram, text, parse_mode="HTML", limit=80)
    assert len(sent) > 1
    assert all(len(chunk) <= 80 for chunk, _mode in sent)
    assert all(mode == "HTML" for _chunk, mode in sent)


def test_send_response_falls_back_to_plain_text_when_html_send_fails(monkeypatch) -> None:
    sent_modes: list[str | None] = []

    def fake_send(text: str, parse_mode: str | None) -> SimpleNamespace:
        sent_modes.append(parse_mode)
        return SimpleNamespace(
            sent=parse_mode is None,
            status_code=400 if parse_mode else 200,
            response_text=(
                '{"ok":false,"description":"Bad Request: can\'t parse entities"}'
                if parse_mode
                else '{"ok":true}'
            ),
            error="telegram_http_400" if parse_mode else "",
        )

    _patch_send_transport(monkeypatch, fake_send)

    assert bot._send_response(_settings().telegram, "<b>hello</b>", parse_mode="HTML")
    assert sent_modes == ["HTML", None]


def test_send_response_does_not_plain_retry_ambiguous_transport_failure(monkeypatch) -> None:
    sent_modes: list[str | None] = []

    def fake_send(_text: str, parse_mode: str | None) -> SimpleNamespace:
        sent_modes.append(parse_mode)
        return SimpleNamespace(
            sent=False,
            status_code=None,
            response_text="",
            error="telegram_url_error:timed out",
        )

    _patch_send_transport(monkeypatch, fake_send)

    assert not bot._send_response(_settings().telegram, "<b>hello</b>", parse_mode="HTML")
    assert sent_modes == ["HTML"]


def test_send_response_splits_text_and_omits_parse_mode_when_none(monkeypatch) -> None:
    sent: list[tuple[str, str | None]] = []

    def fake_send(text: str, parse_mode: str | None) -> bool:
        sent.append((text, parse_mode))
        return True

    _patch_send_transport(monkeypatch, fake_send)

    assert bot._send_response(_settings().telegram, "x" * 5000, parse_mode=None)
    assert len(sent) == 2
    assert len(sent[0][0]) == bot._MAX_MESSAGE_LENGTH
    assert all(mode is None for _text, mode in sent)


def test_api_call_handles_success_api_error_http_error_and_generic_error(monkeypatch) -> None:
    class Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    monkeypatch.setattr(
        telegram_api_mod,
        "urlopen",
        lambda request, *, timeout: Response({"ok": True, "result": {"id": 1}}),
    )
    assert bot._api_call("token", "method") == {"id": 1}

    monkeypatch.setattr(
        telegram_api_mod,
        "urlopen",
        lambda request, *, timeout: Response({"ok": False, "description": "bad"}),
    )
    assert bot._api_call("token", "method") is None

    def raise_http_error(request: object, *, timeout: int) -> object:
        headers: Message[str, str] = Message()
        raise urllib.error.HTTPError(
            url="https://example.test",
            code=429,
            msg="too many",
            hdrs=headers,
            fp=io.BytesIO(b"rate limited"),
        )

    monkeypatch.setattr(telegram_api_mod, "urlopen", raise_http_error)
    assert bot._api_call("token", "method") is None

    monkeypatch.setattr(
        telegram_api_mod,
        "urlopen",
        lambda request, *, timeout: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert bot._api_call("token", "method") is None


def test_set_bot_commands_sends_expected_command_payload(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_api_call(token: str, method: str, payload: dict[str, Any]) -> None:
        captured.update({"token": token, "method": method, "payload": payload})

    monkeypatch.setattr(bot, "_api_call", fake_api_call)

    bot._set_bot_commands("bot-token")

    assert captured["token"] == "bot-token"
    assert captured["method"] == "setMyCommands"
    assert [item["command"] for item in captured["payload"]["commands"]] == [
        "list",
        "cancel",
        "help",
    ]


def test_settings_from_env_uses_autodiscovery(monkeypatch) -> None:
    monkeypatch.setenv("ORCA_AUTO_FLOW_TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("ORCA_AUTO_FLOW_TELEGRAM_CHAT_ID", "chat-id")
    monkeypatch.setattr(bot._activity_sources, "discover_workflow_root", lambda explicit: "/tmp/wf")
    monkeypatch.setattr(
        bot._activity_sources,
        "discover_shared_config",
        lambda explicit: "/tmp/orca_auto.yaml",
    )

    settings = bot.settings_from_env()

    assert settings.telegram.bot_token == "bot-token"
    assert settings.telegram.chat_id == "chat-id"
    assert settings.workflow_root == "/tmp/wf"
    assert settings.crest_config == "/tmp/orca_auto.yaml"
    assert settings.xtb_config == "/tmp/orca_auto.yaml"
    assert settings.orca_config == "/tmp/orca_auto.yaml"


def test_telegram_from_config_path_handles_empty_missing_invalid_and_missing_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not bot._telegram_from_config_path("").enabled
    assert not bot._telegram_from_config_path(str(tmp_path / "missing.yaml")).enabled

    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("telegram: [", encoding="utf-8")
    assert not bot._telegram_from_config_path(str(bad_yaml)).enabled

    non_mapping = tmp_path / "list.yaml"
    non_mapping.write_text("- item\n", encoding="utf-8")
    assert not bot._telegram_from_config_path(str(non_mapping)).enabled

    no_telegram = tmp_path / "no-telegram.yaml"
    no_telegram.write_text("workflow:\n  root: /tmp/wf\n", encoding="utf-8")
    assert not bot._telegram_from_config_path(str(no_telegram)).enabled

    class BadPath:
        def __init__(self, _value: object) -> None:
            pass

        def expanduser(self) -> BadPath:
            raise OSError("bad path")

    monkeypatch.setattr(bot, "Path", BadPath)
    assert not bot._telegram_from_config_path("/bad").enabled


def test_settings_from_config_uses_shared_telegram_section(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                "runs_root: /tmp/workflows",
                "messenger:",
                "  telegram:",
                '    bot_token: "bot-token"',
                '    chat_id: "chat-id"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = bot.settings_from_config(str(config_path))

    assert settings.telegram.bot_token == "bot-token"
    assert settings.telegram.chat_id == "chat-id"
    assert settings.workflow_root == str(Path("/tmp/workflows").resolve())
    assert settings.crest_config == str(config_path.resolve())
    assert settings.xtb_config == str(config_path.resolve())
    assert settings.orca_config == str(config_path.resolve())


def test_settings_from_config_falls_back_to_environment_when_config_telegram_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("workflow:\n  root: /tmp/workflows\n", encoding="utf-8")
    monkeypatch.setenv("ORCA_AUTO_FLOW_TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("ORCA_AUTO_FLOW_TELEGRAM_CHAT_ID", "env-chat")

    settings = bot.settings_from_config(str(config_path))

    assert settings.enabled is True
    assert settings.telegram.bot_token == "env-token"
    assert settings.telegram.chat_id == "env-chat"


def test_run_bot_disabled_settings_returns_error() -> None:
    settings = bot.TelegramBotSettings(
        telegram=TelegramConfig(),
        workflow_root=None,
        crest_config=None,
        xtb_config=None,
        orca_config=None,
        orca_repo_root=None,
    )

    assert bot.run_bot(settings) == 1


def test_legacy_dispatch_helpers_process_known_unknown_and_handler_error_updates(
    monkeypatch,
) -> None:
    settings = _settings()
    sent: list[tuple[str, str | None]] = []
    calls = {"polls": 0}

    def fake_api_call(
        token: str, method: str, payload: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if method == "setMyCommands":
            return True
        if method == "getUpdates":
            calls["polls"] += 1
            if calls["polls"] > 1:
                raise KeyboardInterrupt
            return [
                "skip",
                {"update_id": 1, "message": "skip"},
                {"update_id": 2, "message": {"chat": {"id": "other"}, "text": "/help"}},
                {"update_id": 3, "message": {"chat": {"id": "chat-id"}, "text": "hello"}},
                {"update_id": 4, "message": {"chat": {"id": "chat-id"}, "text": "/unknown"}},
                {"update_id": 5, "message": {"chat": {"id": "chat-id"}, "text": "/list"}},
                {"update_id": 6, "message": {"chat": {"id": "chat-id"}, "text": "/boom"}},
            ]
        return None

    def fake_send_response(
        config: TelegramConfig,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        limit: int = bot._MAX_MESSAGE_LENGTH,
    ) -> bool:
        sent.append((text, parse_mode))
        return True

    monkeypatch.setitem(
        bot._HANDLERS,
        "boom",
        lambda _settings, _args: (_ for _ in ()).throw(RuntimeError("bad <boom>")),
    )
    monkeypatch.setitem(bot._HANDLERS, "list", lambda _settings, _args: "list body")
    monkeypatch.setattr(bot, "_api_call", fake_api_call)
    monkeypatch.setattr(bot, "_send_response", fake_send_response)
    monkeypatch.setattr(bot, "_send_preformatted_response", fake_send_response)

    assert (
        bot._dispatch.run_bot(
            settings,
            settings_from_env_fn=bot.settings_from_env,
            set_bot_commands_fn=bot._set_bot_commands,
            poll_updates_fn=bot._poll_updates,
            dispatch_update_fn=bot._dispatch_update,
            logger=bot.logger,
        )
        == 0
    )
    assert any("Unknown command: /unknown" in text for text, _mode in sent)
    assert any(text == "list body" and mode == "HTML" for text, mode in sent)
    assert any("Error: bad &lt;boom&gt;" in text for text, _mode in sent)


def test_historical_run_bot_with_explicit_settings_uses_canonical_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = bot.TelegramBotSettings(
        telegram=TelegramConfig(bot_token="bot-token", chat_id="123"),
        workflow_root="/tmp/workflows",
        crest_config="/tmp/orca_auto.yaml",
        xtb_config="/tmp/orca_auto.yaml",
        orca_config="/tmp/orca_auto.yaml",
        orca_repo_root=None,
    )
    captured: list[bot.TelegramBotSettings] = []

    def run_canonical(actual: bot.TelegramBotSettings) -> int:
        captured.append(actual)
        return 17

    monkeypatch.setattr(
        bot,
        "_run_canonical_telegram",
        run_canonical,
    )

    assert bot.run_bot(settings) == 17
    assert captured == [settings]
