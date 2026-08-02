from __future__ import annotations

import pytest

from orca_auto import cli as unified_cli
from orca_auto import cli_parsers
from orca_auto.cli_parsers import _orca_auto_version, _suggestion_hint


def test_version_names_the_source_tree_when_the_install_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `--version` is where the frozen number surfaced, so it must not report it
    # bare while the interpreter runs a different source tree.
    monkeypatch.setattr(cli_parsers, "package_version", lambda: "0.1.0")
    monkeypatch.setattr(cli_parsers, "installed_version_drift", lambda: ("0.1.0", "1.0.0"))

    text = _orca_auto_version()

    assert text.startswith("0.1.0")
    assert "1.0.0" in text
    assert "pip install -e ." in text


def test_version_stays_bare_when_the_install_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_parsers, "package_version", lambda: "1.0.0")
    monkeypatch.setattr(cli_parsers, "installed_version_drift", lambda: None)

    assert _orca_auto_version() == "1.0.0"


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
