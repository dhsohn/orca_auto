from __future__ import annotations

import io

import pytest

from orca_auto.core import terminal
from orca_auto.core.activity_icons import activity_status_icon


@pytest.fixture(autouse=True)
def _reset_color_override():
    terminal.set_color_override(None)
    yield
    terminal.set_color_override(None)


def test_activity_status_icon_known_and_fallback() -> None:
    assert activity_status_icon("completed") == "✅"
    assert activity_status_icon("RUNNING") == "▶"
    assert activity_status_icon("cancelled") == "⛔"
    assert activity_status_icon("failed") == "❌"
    assert activity_status_icon("submission_failed") == "❌"
    assert activity_status_icon("repair_blocked") == "❌"
    assert activity_status_icon("submitted") == "📤"
    assert activity_status_icon("mystery") == "•"
    assert activity_status_icon(None) == "•"


def test_color_override_takes_precedence_over_env(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    terminal.set_color_override(True)
    assert terminal.color_enabled() is True
    terminal.set_color_override(False)
    assert terminal.color_enabled() is False


def test_color_enabled_env_and_tty(monkeypatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert terminal.color_enabled(io.StringIO()) is False

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert terminal.color_enabled(io.StringIO()) is True

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # A plain StringIO is not a TTY, so color is disabled by default.
    assert terminal.color_enabled(io.StringIO()) is False


def test_paint_noop_when_disabled_and_wraps_when_enabled() -> None:
    terminal.set_color_override(False)
    assert terminal.paint("hello", terminal.RED) == "hello"

    terminal.set_color_override(True)
    assert terminal.paint("hello", terminal.RED) == "\033[31mhello\033[0m"
    # No codes or empty text is always passed through unchanged.
    assert terminal.paint("hello") == "hello"
    assert terminal.paint("", terminal.RED) == ""


def test_status_color_mapping() -> None:
    assert terminal.status_color("completed") == terminal.GREEN
    assert terminal.status_color("failed") == terminal.RED
    assert terminal.status_color("repair_blocked") == terminal.RED
    assert terminal.status_color("error") == terminal.RED
    assert terminal.status_color("running") == terminal.BLUE
    assert terminal.status_color("unknown-status") is None


def test_emit_error_writes_to_stderr_with_optional_hint(capsys) -> None:
    terminal.set_color_override(False)
    terminal.emit_error("something broke", hint="try again")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: something broke\nhint: try again\n"


def test_emit_error_without_hint(capsys) -> None:
    terminal.set_color_override(False)
    terminal.emit_error("bare message")
    captured = capsys.readouterr()
    assert captured.err == "error: bare message\n"


def test_emit_prefixed_error_uses_shared_stderr_format(capsys) -> None:
    terminal.set_color_override(False)
    terminal.emit_prefixed_error("worker_lock_error", "already running")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "worker_lock_error: already running\n"
