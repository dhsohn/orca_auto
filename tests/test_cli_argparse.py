from __future__ import annotations

import pytest

from orca_auto import cli as unified_cli
from orca_auto._version import package_version
from orca_auto.cli_parsers import _suggestion_hint


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


@pytest.mark.parametrize("limit", ["-1", "2.5", "1e3"])
def test_queue_list_parser_rejects_a_limit_that_is_not_a_non_negative_integer(
    capsys: pytest.CaptureFixture[str],
    limit: str,
) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["queue", "list", "--limit", limit])

    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert "error:" in stderr
    assert "--limit must be a non-negative integer" in stderr


@pytest.mark.parametrize(("limit", "expected"), [("0", 0), ("1", 1), ("25", 25)])
def test_queue_list_parser_accepts_a_non_negative_integer_limit(limit: str, expected: int) -> None:
    args = unified_cli.build_parser().parse_args(["queue", "list", "--limit", limit])

    assert args.limit == expected


@pytest.mark.parametrize("action", ["Clear", "claer"])
def test_queue_list_parser_rejects_an_action_other_than_clear(
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["queue", "list", action])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_main_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        unified_cli.main(["--version"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"orca_auto {package_version()}"
    assert captured.err == ""
