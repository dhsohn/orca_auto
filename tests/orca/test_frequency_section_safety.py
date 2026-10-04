"""The last ``VIBRATIONAL FREQUENCIES`` header decides, even when it prints none.

An OptTS Freq output whose final frequency header is followed by no supported
``cm**-1`` value has no final frequency evidence. An earlier section printed
before that header belongs to another Hessian and must not verify the TS.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import detect_completion_mode
from orca_auto.orca.frequencies import scan_frequency_sections
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.output_status import iter_output_lines
from orca_auto.orca.statuses import AnalyzerStatus
from tests.orca.test_machine_science import _observation
from tests.orca.test_out_analyzer import CONVERGED, ENERGY, NORMAL, _write_out

OPTTS_FREQ_INPUT = "! HF STO-3G OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_HEADER = "VIBRATIONAL FREQUENCIES\n-----------------------\n\n"
_PRIOR = "  0:      -500.00 cm**-1\n  1:      -120.00 cm**-1\n\n"
_GOOD = "  0:      -420.00 cm**-1\n  1:       120.00 cm**-1\n\n"
_EVIDENCE = ENERGY + "\n" + CONVERGED + "\n"
_TERMINATION = NORMAL + "\n"
# What follows the final, empty header before the normal termination line.
_CLOSE_PATHS = {
    "normal-modes": "NORMAL MODES\n------------\n\n",
    "next-header": (
        "CARTESIAN COORDINATES (ANGSTROEM)\n"
        "---------------------------------\n"
        "  H      0.000000    0.000000    0.000000\n"
        "  H      0.000000    0.000000    0.740000\n\n"
    ),
    # Nothing: the termination line is not a frequency, so the section stays
    # open until the end of the output.
    "eof": "",
}


def _receipt_binds(receipt: dict[str, object], data: bytes) -> None:
    assert receipt["status"] == "available"
    assert receipt["bytes"] == len(data)
    assert receipt["byte_sha256"] == hashlib.sha256(data).hexdigest()


def test_optts_freq_input_requires_final_frequency_evidence(tmp_path: Path) -> None:
    inp = tmp_path / "job.inp"
    inp.write_text(OPTTS_FREQ_INPUT, encoding="utf-8")
    mode = detect_completion_mode(inp)
    assert (mode.kind, mode.require_frequency, mode.require_irc) == ("ts", True, False)


@pytest.mark.parametrize("close", sorted(_CLOSE_PATHS))
def test_last_empty_frequency_header_supersedes_an_earlier_section(
    tmp_path: Path, close: str
) -> None:
    out = _EVIDENCE + _HEADER + _GOOD + _HEADER + _CLOSE_PATHS[close] + _TERMINATION
    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.seen
    assert sections.analysis is None

    generation = tmp_path / "generation"
    generation.mkdir()
    observation = _observation(generation, OPTTS_FREQ_INPUT.encode(), out.encode())
    mode = detect_completion_mode(generation / "job.inp")
    analysis = analyze_output(generation / "job.out", mode)
    assert analysis.status == AnalyzerStatus.INCOMPLETE
    assert analysis.reason == "frequency_evidence_missing"
    assert analysis.markers["imaginary_frequency_count"] == 0
    assert analysis.markers["final_frequency_section"] is False

    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "unknown"
    assert science["reason"] == "frequency_evidence_missing"
    assert science["frequencies_available"] is False
    assert science["imaginary_frequency_count"] is None
    assert science["geometry_scope"] == "transition_state"
    assert science["stationary_point"] == "unverified"
    assert observation["lifecycle"]["outcome"] == "uncertain"
    assert observation["handoff"]["status"] == "blocked"
    _receipt_binds(observation["artifacts"]["input"], OPTTS_FREQ_INPUT.encode())
    _receipt_binds(observation["artifacts"]["orca-output"], out.encode())


def test_scanner_end_of_output_closes_an_empty_last_header() -> None:
    # No termination line: the analyzer reports run_incomplete for this output,
    # so only the scanner's own end-of-input close is pinned here.
    sections = scan_frequency_sections(iter_output_lines(_EVIDENCE + _HEADER + _GOOD + _HEADER))
    assert sections.seen
    assert sections.analysis is None


@pytest.mark.parametrize(
    "out",
    [
        _EVIDENCE + _HEADER + _CLOSE_PATHS["normal-modes"] + _TERMINATION,
        _HEADER + _GOOD + _EVIDENCE + _TERMINATION,
    ],
    ids=["lone-empty-header", "superseded-by-later-final-energy"],
)
def test_no_final_frequency_section_controls(tmp_path: Path, out: str) -> None:
    assert scan_frequency_sections(iter_output_lines(out)).analysis is None
    inp = tmp_path / "job.inp"
    inp.write_text(OPTTS_FREQ_INPUT, encoding="utf-8")
    analysis = analyze_output(_write_out(tmp_path, out), detect_completion_mode(inp))
    assert analysis.status == AnalyzerStatus.INCOMPLETE
    assert analysis.reason == "frequency_evidence_missing"
    assert analysis.markers["imaginary_frequency_count"] == 0
    assert analysis.markers["final_frequency_section"] is False


def test_last_printed_section_still_verifies_after_an_earlier_one(tmp_path: Path) -> None:
    out = _EVIDENCE + _HEADER + _PRIOR + _HEADER + _GOOD + _CLOSE_PATHS["normal-modes"]
    out += _TERMINATION
    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.analysis is not None
    assert sections.analysis.frequencies == (-420.0, 120.0)

    observation = _observation(tmp_path, OPTTS_FREQ_INPUT.encode(), out.encode())
    analysis = analyze_output(tmp_path / "job.out", detect_completion_mode(tmp_path / "job.inp"))
    assert analysis.status == AnalyzerStatus.COMPLETED
    assert analysis.reason == "ts_criteria_met"
    assert analysis.markers["imaginary_frequency_count"] == 1
    assert analysis.markers["final_frequency_section"] is True

    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "verified"
    assert science["frequencies_available"] is True
    assert science["imaginary_frequency_count"] == 1
    assert science["stationary_point"] == "first_order_saddle"
    assert observation["lifecycle"]["outcome"] == "succeeded"
    assert observation["handoff"]["status"] == "ready"
    _receipt_binds(observation["artifacts"]["input"], OPTTS_FREQ_INPUT.encode())
    _receipt_binds(observation["artifacts"]["orca-output"], out.encode())


# An indexed row with the supported unit but a value outside the supported
# finite-decimal grammar. Its section must not shrink to the rows before it.
OPTFREQ_INPUT = "! HF STO-3G Opt Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
_INPUTS = {"OptFreq": OPTFREQ_INPUT, "OptTS": OPTTS_FREQ_INPUT}
_UNSUPPORTED_VALUES = ["NaN", "Inf", "+Inf", "-Inf"]


def _rows(*values: str) -> str:
    return "".join(f"  {index}:  {value:>12} cm**-1\n" for index, value in enumerate(values)) + "\n"


def _final_section(rows: str) -> str:
    return _EVIDENCE + rows + _CLOSE_PATHS["normal-modes"] + _TERMINATION


def test_optfreq_input_requires_final_frequency_evidence(tmp_path: Path) -> None:
    inp = tmp_path / "job.inp"
    inp.write_text(OPTFREQ_INPUT, encoding="utf-8")
    mode = detect_completion_mode(inp)
    assert (mode.kind, mode.require_frequency, mode.require_irc) == ("opt", True, False)


@pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
@pytest.mark.parametrize("job", sorted(_INPUTS))
def test_unsupported_value_row_makes_the_final_section_unavailable(
    tmp_path: Path, job: str, value: str
) -> None:
    out = _final_section(_HEADER + _rows("100.00", value, "-400.00"))
    observation = _observation(tmp_path, _INPUTS[job].encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "unknown"
    assert science["reason"] == "frequency_evidence_missing"
    assert science["frequencies_available"] is False
    assert science["imaginary_frequency_count"] is None
    assert science["stationary_point"] == "unverified"
    assert observation["lifecycle"]["outcome"] == "uncertain"
    assert observation["handoff"]["status"] == "blocked"
    _receipt_binds(observation["artifacts"]["input"], _INPUTS[job].encode())
    _receipt_binds(observation["artifacts"]["orca-output"], out.encode())

    analysis = analyze_output(tmp_path / "job.out", detect_completion_mode(tmp_path / "job.inp"))
    assert analysis.status == AnalyzerStatus.INCOMPLETE
    assert analysis.reason == "frequency_evidence_missing"
    assert analysis.markers["imaginary_frequency_count"] == 0
    assert analysis.markers["final_frequency_section"] is False

    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.seen
    assert sections.analysis is None


@pytest.mark.parametrize(
    ("job", "stationary"),
    [("OptFreq", "unverified"), ("OptTS", "first_order_saddle")],
)
def test_supported_negative_middle_row_is_counted(
    tmp_path: Path, job: str, stationary: str
) -> None:
    out = _final_section(_HEADER + _rows("100.00", "200.00", "-400.00"))
    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.analysis is not None
    assert sections.analysis.frequencies == (100.0, 200.0, -400.0)

    observation = _observation(tmp_path, _INPUTS[job].encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "verified"
    assert science["frequencies_available"] is True
    assert science["imaginary_frequency_count"] == 1
    assert science["stationary_point"] == stationary
    assert observation["lifecycle"]["outcome"] == "succeeded"
    assert observation["handoff"]["status"] == "ready"


def test_unsupported_final_section_does_not_fall_back_to_an_earlier_one(tmp_path: Path) -> None:
    out = _final_section(
        _HEADER + _rows("150.00", "250.00") + _HEADER + _rows("100.00", "NaN", "-400.00")
    )
    observation = _observation(tmp_path, OPTFREQ_INPUT.encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "unknown"
    assert science["frequencies_available"] is False
    assert science["stationary_point"] == "unverified"
    assert observation["lifecycle"]["outcome"] == "uncertain"
    assert observation["handoff"]["status"] == "blocked"
    assert scan_frequency_sections(iter_output_lines(out)).analysis is None


def test_later_supported_section_recovers_after_an_unsupported_one(tmp_path: Path) -> None:
    out = _final_section(
        _HEADER + _rows("100.00", "NaN", "-400.00") + _HEADER + _rows("120.00", "300.00")
    )
    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.analysis is not None
    assert sections.analysis.frequencies == (120.0, 300.0)

    observation = _observation(tmp_path, OPTFREQ_INPUT.encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "verified"
    assert science["imaginary_frequency_count"] == 0
    assert science["stationary_point"] == "minimum"
    assert observation["handoff"]["status"] == "ready"


# Rows that print the supported unit without a supported value, in shapes the
# finite grammar itself accepts: first in the section, without a mode index, and
# with no space before the unit; and a finite prefix holding one negative mode.
_MALFORMED_SECTIONS = {
    "first-row": (
        "  0:           NaN cm**-1",
        "  1:       100.00 cm**-1",
        "  2:      -400.00 cm**-1",
    ),
    "unindexed": (
        "  0:       100.00 cm**-1",
        "               NaN cm**-1",
        "  2:      -400.00 cm**-1",
    ),
    "no-space-unit": (
        "  0:       100.00 cm**-1",
        "  1:           NaNcm**-1",
        "  2:      -400.00 cm**-1",
    ),
    "negative-prefix": (
        "  0:      -400.00 cm**-1",
        "  1:           NaN cm**-1",
        "  2:       100.00 cm**-1",
    ),
}


@pytest.mark.parametrize("shape", sorted(_MALFORMED_SECTIONS))
@pytest.mark.parametrize("job", sorted(_INPUTS))
def test_any_unparsed_supported_unit_row_makes_the_final_section_unavailable(
    tmp_path: Path, job: str, shape: str
) -> None:
    out = _final_section(_HEADER + "\n".join(_MALFORMED_SECTIONS[shape]) + "\n\n")
    observation = _observation(tmp_path, _INPUTS[job].encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "unknown"
    assert science["reason"] == "frequency_evidence_missing"
    assert science["frequencies_available"] is False
    assert science["imaginary_frequency_count"] is None
    assert science["stationary_point"] == "unverified"
    assert observation["lifecycle"]["outcome"] == "uncertain"
    assert observation["handoff"]["status"] == "blocked"
    _receipt_binds(observation["artifacts"]["input"], _INPUTS[job].encode())
    _receipt_binds(observation["artifacts"]["orca-output"], out.encode())

    analysis = analyze_output(tmp_path / "job.out", detect_completion_mode(tmp_path / "job.inp"))
    assert analysis.status == AnalyzerStatus.INCOMPLETE
    assert analysis.reason == "frequency_evidence_missing"
    assert analysis.markers["imaginary_frequency_count"] == 0
    assert analysis.markers["final_frequency_section"] is False

    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.seen
    assert sections.analysis is None


def test_scaling_prose_without_the_unit_leaves_the_section_intact(tmp_path: Path) -> None:
    scaling = "Scaling factor for frequencies =  1.000000000  (already applied!)\n\n"
    out = _final_section(_HEADER + scaling + _rows("120.00", "300.00"))
    sections = scan_frequency_sections(iter_output_lines(out))
    assert sections.analysis is not None
    assert sections.analysis.frequencies == (120.0, 300.0)

    observation = _observation(tmp_path, OPTFREQ_INPUT.encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "verified"
    assert science["stationary_point"] == "minimum"
    assert observation["handoff"]["status"] == "ready"
