from __future__ import annotations

import pytest

from orca_auto import cli as unified_cli
from orca_auto.cli_argparse import _suggestion_hint


def test_suggestion_hint_offers_close_match() -> None:
    message = "argument command: invalid choice: 'queu' (choose from 'queue', 'run-dir')"
    assert _suggestion_hint(message) == "did you mean `queue`?"


def test_suggestion_hint_lists_choices_when_no_close_match() -> None:
    message = "argument command: invalid choice: 'zzz' (choose from 'queue', 'run-dir')"
    hint = _suggestion_hint(message)
    assert hint is not None
    assert "valid choices: queue, run-dir" == hint


def test_suggestion_hint_ignores_unrelated_messages() -> None:
    assert _suggestion_hint("the following arguments are required: path") is None


def test_parser_error_suggests_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = unified_cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["queu"])
    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert "error:" in stderr
    assert "did you mean `queue`?" in stderr


def test_scan_notify_parses_canonical_command() -> None:
    parser = unified_cli.build_parser()
    scan_args = parser.parse_args(["scan-notify", "--orca_auto-config", "/tmp/orca_auto.yaml"])
    assert scan_args.command == "scan-notify"
