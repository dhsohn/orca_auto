from __future__ import annotations

import io

import pytest

from orca_auto import cli_errors, cli_style
from orca_auto.core.activity_icons import activity_status_icon


@pytest.fixture(autouse=True)
def _reset_color_override():
    cli_style.set_color_override(None)
    yield
    cli_style.set_color_override(None)


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
    cli_style.set_color_override(True)
    assert cli_style.color_enabled() is True
    cli_style.set_color_override(False)
    assert cli_style.color_enabled() is False


def test_color_enabled_env_and_tty(monkeypatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli_style.color_enabled(io.StringIO()) is False

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert cli_style.color_enabled(io.StringIO()) is True

    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # A plain StringIO is not a TTY, so color is disabled by default.
    assert cli_style.color_enabled(io.StringIO()) is False


def test_paint_noop_when_disabled_and_wraps_when_enabled() -> None:
    cli_style.set_color_override(False)
    assert cli_style.paint("hello", cli_style.RED) == "hello"

    cli_style.set_color_override(True)
    assert cli_style.paint("hello", cli_style.RED) == "\033[31mhello\033[0m"
    # No codes or empty text is always passed through unchanged.
    assert cli_style.paint("hello") == "hello"
    assert cli_style.paint("", cli_style.RED) == ""


def test_status_color_mapping() -> None:
    assert cli_style.status_color("completed") == cli_style.GREEN
    assert cli_style.status_color("failed") == cli_style.RED
    assert cli_style.status_color("repair_blocked") == cli_style.RED
    assert cli_style.status_color("error") == cli_style.RED
    assert cli_style.status_color("running") == cli_style.BLUE
    assert cli_style.status_color("unknown-status") is None


def test_emit_error_writes_to_stderr_with_optional_hint(capsys) -> None:
    cli_style.set_color_override(False)
    cli_errors.emit_error("something broke", hint="try again")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: something broke\nhint: try again\n"


def test_emit_error_without_hint(capsys) -> None:
    cli_style.set_color_override(False)
    cli_errors.emit_error("bare message")
    captured = capsys.readouterr()
    assert captured.err == "error: bare message\n"


def test_emit_prefixed_error_uses_shared_stderr_format(capsys) -> None:
    cli_style.set_color_override(False)
    cli_errors.emit_prefixed_error("worker_lock_error", "already running")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "worker_lock_error: already running\n"
