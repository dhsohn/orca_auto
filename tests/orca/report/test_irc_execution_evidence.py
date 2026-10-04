"""IRC report evidence comes from execution lines of the final output only.

Every output here is SYNTHETIC: ``write_irc_out`` / ``IRC_BLOCK`` from
``tests/orca_output_helpers.py`` (a bounded imitation of the ORCA 6 IRC driver
output) plus small literal echo, comment and driver lines. No authentic ORCA
bytes are used.

The "IRC path found" badge means that an execution line of the final output
shows the IRC driver or its path-summary header; it is not path convergence
and not endpoint certification. Input echoes (``|  N>``) and comments (``#``)
never count, lines split only at CR, LF and CRLF (``output_status``), and
neither the badge nor the IRC setup, iterations or path profile are borrowed
from another attempt's output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.completion_rules import detect_completion_mode
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.report.composer import collect_html_report_parts
from orca_auto.orca.report.irc import IrcReportData, parse_irc_output, parse_irc_output_text
from orca_auto.orca.report.publication import write_job_html_report
from tests.engine_artifact_helpers import report_generation_target
from tests.orca_output_helpers import write_irc_inp, write_irc_out

_IRC_ROUTE = "! B3LYP def2-SVP IRC"
_TS_IRC_ROUTE = "! OptTS Freq IRC B3LYP def2-SVP"
_CONSTRAINTS = "%geom Constraints { B 0 1 C } end end"
_TERMINATED = "                             ****ORCA TERMINATED NORMALLY****\n"
# Synthetic stand-in for a driver banner: the analyzer's IRC_DRIVER_NEEDLE on
# an execution line, with no settings block and no path-summary table.
_DRIVER_ONLY_LINE = "                         IRC-DRV: starting the IRC driver\n"
_PATH_SUMMARY_TABLE = (
    "IRC PATH SUMMARY\n"
    "Step        E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)\n"
    "   1     -344.045000   -29.947000    0.001100  0.000550\n"
    "   2     -343.997280     0.000000    0.000200  0.000033 <= TS\n"
)


def _no_irc_out(path: Path) -> None:
    """A synthetic IRC-route output whose driver never ran (no IRC block)."""
    write_irc_out(path, route=_IRC_ROUTE, irc_block="")


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")


def _attempt(index: int, out_path: Path, *, status: str, reason: str) -> dict[str, Any]:
    return {
        "index": index,
        "inp_path": "",
        "out_path": str(out_path),
        "return_code": 0,
        "analyzer_status": status,
        "analyzer_reason": reason,
        "markers": {},
        "patch_actions": [],
        "started_at": f"2026-07-07T0{index}:00:00+00:00",
        "ended_at": f"2026-07-07T0{index}:12:00+00:00",
    }


def _state(
    reaction_dir: Path,
    outputs: list[Path],
    *,
    final: Path,
    status: str = "completed",
    reason: str = "normal_termination",
) -> dict[str, Any]:
    analyzer_status = "completed" if status == "completed" else "incomplete"
    attempts = [
        _attempt(index, out, status=analyzer_status, reason=reason)
        for index, out in enumerate(outputs, start=1)
    ]
    for attempt in attempts:
        attempt["inp_path"] = str(reaction_dir / "rxn.inp")
    return {
        "job_id": "job_irc_evidence",
        "run_id": "run_irc_evidence",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "status": status,
        "started_at": "2026-07-07T01:00:00+00:00",
        "updated_at": "2026-07-07T09:00:00+00:00",
        "attempts": attempts,
        "final_result": {
            "status": status,
            "analyzer_status": analyzer_status,
            "reason": reason,
            "completed_at": "2026-07-07T09:00:00+00:00",
            "last_out_path": str(final),
        },
    }


def _irc_data(reaction_dir: Path, state: dict[str, Any]) -> IrcReportData:
    parts = collect_html_report_parts(reaction_dir, state)
    assert parts is not None and parts.irc is not None
    return parts.irc


def _html(reaction_dir: Path, state: dict[str, Any]) -> str:
    path = write_job_html_report(
        reaction_dir, state, generation_target=report_generation_target(reaction_dir)
    )
    assert path is not None, "the diagnostic IRC report must still be published"
    return path.read_text(encoding="utf-8")


# --- Text-level filter: echoes and comments never establish IRC facts --------


@pytest.mark.parametrize(
    "text",
    [
        "|  1> ! IRC  # IRC PATH SUMMARY\n",
        "|  7> IRC PATH SUMMARY\n",
        "# IRC PATH SUMMARY\n",
        "   # IRC-DRV\n",
        "|  3> %irc IRC-DRV end\n",
        # A Unicode line separator does not start a new output line.
        "# note IRC PATH SUMMARY\n",
        "|  4> x IRC-DRV\n",
        # CRLF and CR end lines; the commented line stays commented.
        "# IRC PATH SUMMARY\r\nplain text\r\n",
        "# IRC-DRV\rplain text\r",
    ],
    ids=[
        "echo-comment",
        "echo",
        "comment",
        "indented-comment-driver",
        "echo-driver",
        "comment-line-separator",
        "echo-paragraph-separator",
        "comment-crlf",
        "comment-cr",
    ],
)
def test_echoed_or_commented_markers_are_not_irc_driver_evidence(text: str) -> None:
    parsed = parse_irc_output_text(text + _TERMINATED)

    assert parsed.irc_marker_found is False
    assert parsed.path_points == ()
    assert parsed.iterations == ()
    assert parsed.settings == ()


def test_echoed_or_commented_settings_iterations_and_path_rows_are_ignored() -> None:
    text = "".join(
        [
            "|  5> Intrinsic Reaction Coordinate Calculation\n",
            "|  6> Max. no of cycles        MaxIter    .... 30\n",
            "# FORWARD IRC\n",
            "#     0     -343.997280    0.000000    0.002000  0.000900\n",
            *(f"|  9> {line}\n" for line in _PATH_SUMMARY_TABLE.splitlines()),
            "# note Intrinsic Reaction Coordinate Calculation Nr. of atoms  .... 2\n",
            _TERMINATED,
        ]
    )

    parsed = parse_irc_output_text(text)

    assert parsed.settings == ()
    assert parsed.iterations == ()
    assert parsed.path_points == ()
    assert parsed.irc_marker_found is False


@pytest.mark.parametrize(
    "text",
    [
        "IRC PATH SUMMARY\n",
        "irc path summary\n",
        _DRIVER_ONLY_LINE,
        "|  1> ! IRC\rIRC PATH SUMMARY\r",
        "|  1> ! IRC\r\nIRC-DRV\r\n",
    ],
    ids=["header", "lower-case", "driver-only", "cr-after-echo", "crlf-after-echo"],
)
def test_execution_line_markers_are_irc_driver_evidence(text: str) -> None:
    assert parse_irc_output_text(text + _TERMINATED).irc_marker_found is True


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_real_irc_block_is_read_under_every_supported_newline(tmp_path: Path, newline: str) -> None:
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route=_IRC_ROUTE)
    text = out_path.read_text(encoding="utf-8").replace("\n", newline)

    parsed = parse_irc_output_text(text)

    assert parsed.irc_marker_found is True
    assert len(parsed.path_points) == 5
    assert len(parsed.iterations) == 5
    assert any(setting.value == "job_IRC_Full_trj.xyz" for setting in parsed.settings)


def test_echoed_marker_beside_a_real_irc_block_keeps_the_real_evidence(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route=_IRC_ROUTE)
    _append(out_path, "|  9> # IRC PATH SUMMARY\n# IRC-DRV\n")

    parsed = parse_irc_output(out_path)

    assert parsed.irc_marker_found is True
    assert len(parsed.path_points) == 5
    assert [point.marker for point in parsed.path_points].count("TS") == 1


# --- Agreement with the completion analyzer on the same bytes ----------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("", False),
        ("|  7> IRC PATH SUMMARY\n", False),
        ("# IRC-DRV\n", False),
        ("# note IRC PATH SUMMARY\n", False),
        (_DRIVER_ONLY_LINE, True),
        ("IRC PATH SUMMARY\n", True),
        ("                       IRC PATH SUMMARY              \n", True),
        ("irc path summary\n", True),
        # The analyzer matches the literal upper-case needles inside one
        # execution line: other spacing or a line break is no marker, while a
        # glued prefix or suffix still contains the needle.
        ("IRC  PATH SUMMARY\n", False),
        ("IRC\nPATH SUMMARY\n", False),
        ("XIRC PATH SUMMARY\n", True),
        ("IRC PATH SUMMARYX\n", True),
        ("|  3> %irc IRC-DRV end\n", False),
    ],
    ids=[
        "no-driver",
        "echo",
        "comment",
        "comment-separator",
        "driver-only",
        "header",
        "padded-canonical-header",
        "lower-case",
        "double-space",
        "split-newline",
        "glued-prefix",
        "glued-suffix",
        "echo-driver",
    ],
)
def test_badge_agrees_with_the_analyzer_marker_on_the_same_output(
    tmp_path: Path, extra: str, expected: bool
) -> None:
    write_irc_inp(tmp_path / "rxn.inp", _IRC_ROUTE)
    out_path = tmp_path / "rxn.out"
    _no_irc_out(out_path)
    _append(out_path, extra)

    verdict = analyze_output(out_path, detect_completion_mode(tmp_path / "rxn.inp"))

    assert verdict.markers["irc_marker_found"] is expected
    assert parse_irc_output(out_path).irc_marker_found is expected
    if not expected:
        assert verdict.reason == "irc_evidence_missing"


def test_real_irc_output_agrees_with_the_analyzer_marker(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", _IRC_ROUTE)
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route=_IRC_ROUTE)

    verdict = analyze_output(out_path, detect_completion_mode(tmp_path / "rxn.inp"))

    assert verdict.markers["irc_marker_found"] is True
    assert parse_irc_output(out_path).irc_marker_found is True


# --- Report binding: primary IRC facts come from the final output only -------


def test_earlier_real_attempt_never_lends_the_final_output_a_badge_or_profile(
    tmp_path: Path,
) -> None:
    write_irc_inp(tmp_path / "rxn.inp", _IRC_ROUTE)
    earlier = tmp_path / "rxn.out"
    write_irc_out(earlier, route=_IRC_ROUTE)
    final = tmp_path / "rxn_final.out"
    _no_irc_out(final)
    _append(final, "|  9> # IRC PATH SUMMARY\n")
    state = _state(
        tmp_path, [earlier, final], final=final, status="failed", reason="irc_evidence_missing"
    )

    data = _irc_data(tmp_path, state)

    assert data.irc_marker_found is False
    assert data.path_points == ()
    assert data.iterations == ()
    assert data.settings == ()
    # Each attempt row still shows its own output's history.
    assert data.attempts[0].detail == "5 path pts, 5 IRC iter"
    assert data.attempts[1].detail == ""

    html = _html(tmp_path, state)
    assert "IRC path found" not in html
    assert "irc_evidence_missing" in html
    assert "No IRC path-summary points were parsed from the final output" in html
    assert "<code>rxn_final.out</code>" in html


def test_final_real_output_gets_the_badge_whatever_earlier_attempts_showed(
    tmp_path: Path,
) -> None:
    write_irc_inp(tmp_path / "rxn.inp", _IRC_ROUTE)
    earlier = tmp_path / "rxn.out"
    _no_irc_out(earlier)
    _append(earlier, "|  9> IRC PATH SUMMARY\n")
    final = tmp_path / "rxn_final.out"
    write_irc_out(final, route=_IRC_ROUTE)
    state = _state(tmp_path, [earlier, final], final=final)

    data = _irc_data(tmp_path, state)

    assert data.irc_marker_found is True
    assert len(data.path_points) == 5
    assert data.attempts[0].detail == ""
    assert data.attempts[1].detail == "5 path pts, 5 IRC iter"
    assert "IRC path found" in _html(tmp_path, state)


def test_driver_only_final_output_gets_the_badge_without_a_profile(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", _IRC_ROUTE)
    final = tmp_path / "rxn.out"
    _no_irc_out(final)
    _append(final, _DRIVER_ONLY_LINE)
    state = _state(tmp_path, [final], final=final)

    data = _irc_data(tmp_path, state)

    assert data.irc_marker_found is True
    assert data.path_points == ()
    html = _html(tmp_path, state)
    # Driver presence only: the page shows no path and claims no convergence.
    assert "IRC path found" in html
    assert "No IRC path-summary points were parsed" in html


@pytest.mark.parametrize("final_kind", ["missing", "unreadable"])
def test_missing_or_unreadable_final_output_has_no_fallback_evidence(
    tmp_path: Path, final_kind: str
) -> None:
    write_irc_inp(tmp_path / "rxn.inp", _IRC_ROUTE)
    earlier = tmp_path / "rxn.out"
    write_irc_out(earlier, route=_IRC_ROUTE)
    final = tmp_path / "rxn_final.out"
    if final_kind == "unreadable":
        # A directory where the final output should be: it exists but cannot
        # be read as output text.
        final.mkdir()
    state = _state(
        tmp_path, [earlier, final], final=final, status="failed", reason="output_read_error"
    )

    data = _irc_data(tmp_path, state)

    assert data.irc_marker_found is False
    assert data.path_points == ()
    assert data.settings == ()
    assert data.attempts[0].detail == "5 path pts, 5 IRC iter"
    html = _html(tmp_path, state)
    assert "IRC path found" not in html
    assert "output_read_error" in html


# --- Constrained TS search with IRC: the IRC card makes no saddle claim -------


@pytest.mark.parametrize("constrained", [True, False], ids=["constrained", "unconstrained"])
def test_ts_irc_imaginary_card_bounds_a_constrained_search(
    tmp_path: Path, constrained: bool
) -> None:
    route = f"{_TS_IRC_ROUTE}\n{_CONSTRAINTS}" if constrained else _TS_IRC_ROUTE
    write_irc_inp(tmp_path / "rxn.inp", route)
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route=_TS_IRC_ROUTE, freq=True, opt=True)
    state = _state(tmp_path, [out_path], final=out_path, reason="ts_criteria_met")

    data = _irc_data(tmp_path, state)
    assert data.ts_route is True
    assert data.imaginary_count == 1

    html = _html(tmp_path, state)
    # Still a TS search with an IRC: operation identity and numbers stay.
    assert "IRC report" in html
    assert "TS optimization convergence" in html
    assert "TS opt cycles" in html
    assert "-500.0" in html
    assert "IRC path found" in html
    if constrained:
        assert "constrained TS search: first-order saddle unverified" in html
        assert "expected 1" not in html
    else:
        assert "expected 1" in html
        assert "constrained TS search" not in html
