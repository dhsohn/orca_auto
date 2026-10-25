from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.output_status import termination_line
from orca_auto.orca.statuses import AnalyzerStatus

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_UNKNOWN_IDENTIFIER = "\t Unknown identifier in IRC block line   11  : "


def test_native_unknown_input_block_identifier_is_an_error_termination() -> None:
    output = _FIXTURES / "orca_input_errors" / "unknown_irc_keyword.out"

    result = analyze_output(output, CompletionMode(kind="irc", require_irc=True))

    assert result.status is AnalyzerStatus.UNKNOWN_FAILURE
    assert result.reason == "error_termination"
    assert result.markers["generic_error_termination"] is True
    assert result.markers["terminated_normally"] is False


@pytest.mark.parametrize(
    "line",
    [
        _UNKNOWN_IDENTIFIER,
        "Unknown identifier in GEOM block line 7 :\n",
        "  unknown identifier in irc block line 11:\r\n",
    ],
)
def test_unknown_input_block_identifier_is_terminal_error_evidence(line: str) -> None:
    assert termination_line(line) == (False, True)


@pytest.mark.parametrize(
    "line",
    [
        "# " + _UNKNOWN_IDENTIFIER,
        "  | 11> " + _UNKNOWN_IDENTIFIER,
        "See diagnostic: " + _UNKNOWN_IDENTIFIER,
        _UNKNOWN_IDENTIFIER + " is an example",
        "Unknown identifier in the input",
        "Last token : INIT_DISPL_DE",
    ],
)
def test_quoted_or_incomplete_input_error_does_not_fail_a_successful_output(
    tmp_path: Path, line: str
) -> None:
    assert termination_line(line) == (False, False)
    output = tmp_path / "h2.out"
    native_output = (_FIXTURES / "orca_6_1_1" / "h2_sp.out").read_text(encoding="utf-8")
    output.write_text(native_output + "\n" + line + "\n", encoding="utf-8")

    result = analyze_output(output, CompletionMode(kind="sp", require_irc=False))

    assert result.status is AnalyzerStatus.COMPLETED
    assert result.reason == "normal_termination"
    assert result.markers["generic_error_termination"] is False
