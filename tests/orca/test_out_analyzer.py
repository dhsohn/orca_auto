from __future__ import annotations

from pathlib import Path
from typing import TextIO

import pytest

from orca_auto.orca import out_analyzer
from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.evidence import parsed_frequency_analysis
from orca_auto.orca.out_analyzer import (
    OutAnalysis,
    analyze_output,
    apply_exit_code,
    scan_ts_lines_for_imag_count,
)
from orca_auto.orca.output_status import iter_output_lines, termination_line
from orca_auto.orca.parser.io import open_orca_text
from orca_auto.orca.statuses import AnalyzerStatus
from tests.integration.test_parser_realistic import (
    _B3LYP_OPT_FREQ_COMPLETED,
    _TS_OPT_WITH_IMAGINARY,
    _TS_REAL_VIB_FORMAT,
)
from tests.orca.test_evidence import _FREQUENCIES as _EVIDENCE_FREQUENCIES
from tests.orca_output_helpers import FREQ_TS_BLOCK, si_out_text

NORMAL = "****ORCA TERMINATED NORMALLY****"
_OPT_MODE = CompletionMode(kind="opt", require_irc=False)
_TS_MODE = CompletionMode(kind="ts", require_irc=False)
_TS_IRC_MODE = CompletionMode(kind="ts", require_irc=True)
_TS_FREQ_MODE = CompletionMode(kind="ts", require_irc=False)


def _write_out(tmp_path: Path, payload: str) -> Path:
    out = tmp_path / "a.out"
    out.write_text(payload, encoding="utf-8")
    return out


def test_status_is_analyzer_status_enum(tmp_path: Path) -> None:
    result = analyze_output(_write_out(tmp_path, NORMAL + "\n"), _OPT_MODE)
    assert isinstance(result.status, AnalyzerStatus)
    assert result.status == AnalyzerStatus.COMPLETED


def test_completed_ts(tmp_path: Path) -> None:
    payload = "\n".join(["some line -123.45 cm**-1", "IRC PATH SUMMARY", NORMAL])
    result = analyze_output(_write_out(tmp_path, payload), _TS_IRC_MODE)
    assert result.status == "completed"


def test_ts_verdict_reads_the_output_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = "\n".join(["VIBRATIONAL FREQUENCIES", "  -120.00 cm**-1", "  140.00 cm**-1", NORMAL])
    out = _write_out(tmp_path, payload)
    opened: list[Path] = []

    def counting_open(path: Path) -> TextIO:
        opened.append(path)
        return open_orca_text(path)

    monkeypatch.setattr(out_analyzer, "open_orca_text", counting_open)
    result = analyze_output(out, _TS_MODE)
    assert result.status == AnalyzerStatus.COMPLETED
    assert result.markers["imaginary_frequency_count"] == 1
    assert opened == [out]


def test_completed_ts_with_irc_marker_outside_tail_window(tmp_path: Path) -> None:
    filler = ("X" * 120 + "\n") * 4000
    payload = "\n".join(["IRC PATH SUMMARY", filler, "some line -123.45 cm**-1", NORMAL])
    result = analyze_output(_write_out(tmp_path, payload), _TS_IRC_MODE)
    assert result.status == "completed"
    assert result.markers["irc_marker_found"]


def test_ts_uses_last_vibrational_frequency_section(tmp_path: Path) -> None:
    payload = "\n".join(
        [
            "VIBRATIONAL FREQUENCIES",
            "  1   -500.00 cm**-1",
            "  2   -120.00 cm**-1",
            "VIBRATIONAL FREQUENCIES",
            "  1   -150.00 cm**-1",
            "  2    120.00 cm**-1",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_MODE)
    assert result.status == "completed"
    assert result.markers["imaginary_frequency_count"] == 1


def test_ts_frequency_section_before_the_final_energy_verifies_nothing(tmp_path: Path) -> None:
    # OptTS with Calc_Hess but without Freq: the only frequency section is
    # the initial Hessian's, printed before the optimization ran.
    payload = "\n".join(
        [
            "VIBRATIONAL FREQUENCIES",
            "  1   -650.00 cm**-1",
            "  2    120.00 cm**-1",
            "FINAL SINGLE POINT ENERGY      -100.100000000000",
            "                    ***        THE OPTIMIZATION HAS CONVERGED      ***",
            "FINAL SINGLE POINT ENERGY      -100.200000000000",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_MODE)
    assert result.status == AnalyzerStatus.TS_NOT_FOUND
    assert result.reason == "ts_criteria_failed"
    assert result.markers["imaginary_frequency_count"] == 0
    assert not result.markers["final_frequency_section"]


def test_ts_counts_only_the_frequency_section_after_the_last_final_energy(
    tmp_path: Path,
) -> None:
    # OptTS Freq with Recalc_Hess: recalculated Hessians print sections
    # mid-optimization; the final Freq follows the last final energy.
    payload = "\n".join(
        [
            "VIBRATIONAL FREQUENCIES",
            "  1   -650.00 cm**-1",
            "  2   -120.00 cm**-1",
            "FINAL SINGLE POINT ENERGY      -100.100000000000",
            "VIBRATIONAL FREQUENCIES",
            "  1   -600.00 cm**-1",
            "  2   -110.00 cm**-1",
            "FINAL SINGLE POINT ENERGY      -100.200000000000",
            "VIBRATIONAL FREQUENCIES",
            "  1   -420.00 cm**-1",
            "  2    120.00 cm**-1",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_FREQ_MODE)
    assert result.status == AnalyzerStatus.COMPLETED
    assert result.reason == "ts_criteria_met"
    assert result.markers["imaginary_frequency_count"] == 1
    assert result.markers["final_frequency_section"]


def test_ts_rejected_for_two_modes_keeps_the_final_section_count(tmp_path: Path) -> None:
    payload = "\n".join(
        [
            "FINAL SINGLE POINT ENERGY      -100.200000000000",
            "VIBRATIONAL FREQUENCIES",
            "  1   -420.00 cm**-1",
            "  2   -120.00 cm**-1",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_FREQ_MODE)
    assert result.status == AnalyzerStatus.TS_NOT_FOUND
    assert result.markers["imaginary_frequency_count"] == 2
    assert result.markers["final_frequency_section"]


@pytest.mark.parametrize(
    ("failure_line", "expected_status"),
    [
        ("THE OPTIMIZATION DID NOT CONVERGE", AnalyzerStatus.GEOM_NOT_CONVERGED),
        ("SCF NOT CONVERGED", AnalyzerStatus.ERROR_SCF),
    ],
    ids=["geometry", "scf"],
)
def test_ts_final_section_is_not_a_verdict_when_the_run_failed(
    tmp_path: Path, failure_line: str, expected_status: AnalyzerStatus
) -> None:
    # ORCA can continue to the Freq step after MaxIter and terminate
    # normally; the section then describes a non-stationary geometry.
    payload = "\n".join(
        [
            failure_line,
            "FINAL SINGLE POINT ENERGY      -100.200000000000",
            "VIBRATIONAL FREQUENCIES",
            "  1   -420.00 cm**-1",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_FREQ_MODE)
    assert result.status == expected_status
    assert result.markers["imaginary_frequency_count"] == 1
    assert not result.markers["final_frequency_section"]


def test_ts_legacy_headerless_count_is_not_a_final_section(tmp_path: Path) -> None:
    payload = "\n".join(
        [
            "FINAL SINGLE POINT ENERGY      -100.200000000000",
            "some line   -420.00 cm**-1",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_FREQ_MODE)
    assert result.status == AnalyzerStatus.COMPLETED
    assert result.markers["imaginary_frequency_count"] == 1
    assert not result.markers["final_frequency_section"]


def test_ts_line_rule_matches_the_workflow_recount_across_a_form_feed(tmp_path: Path) -> None:
    # The analyzer and the recount both iterate the file. ``str.splitlines()``
    # breaks on a form feed and file iteration does not, so a run whose output
    # carries one inside a frequency section would otherwise be sectioned one
    # way by the analyzer and the other way by the recount.
    payload = "\n".join(
        [
            "FINAL SINGLE POINT ENERGY      -100.200000000000",
            "VIBRATIONAL FREQUENCIES",
            "  1   -420.00 cm**-1",
            "  2    120.00 cm**-1",
            "restart log\x0cFINAL SINGLE POINT ENERGY      -100.300000000000",
            NORMAL,
        ]
    )
    out = _write_out(tmp_path, payload)
    result = analyze_output(out, _TS_FREQ_MODE)
    with open_orca_text(out) as handle:
        recount = scan_ts_lines_for_imag_count(handle)
    assert recount == (1, True)
    assert result.reason == "ts_criteria_met"
    assert result.markers["imaginary_frequency_count"] == recount[0]
    assert result.markers["final_frequency_section"] == recount[1]


def test_ts_ignores_tiny_negative_modes(tmp_path: Path) -> None:
    payload = "\n".join(
        [
            "VIBRATIONAL FREQUENCIES",
            "  1     -5.00 cm**-1",
            "  2   -450.00 cm**-1",
            "  3    120.00 cm**-1",
            NORMAL,
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_MODE)
    assert result.status == AnalyzerStatus.COMPLETED
    assert result.markers["imaginary_frequency_count"] == 1


def test_ts_not_found(tmp_path: Path) -> None:
    payload = "\n".join([NORMAL, "TOTAL RUN TIME: 0 days 0 hours 1 minutes 0 seconds"])
    result = analyze_output(_write_out(tmp_path, payload), _TS_MODE)
    assert result.status == "ts_not_found"


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_reason"),
    [
        (
            "Error : multiplicity (1) is odd and number of electrons (235) is odd -> impossible",
            "error_multiplicity_impossible",
            None,
        ),
        ("ORCA finished by error termination in SCF gradient", "error_scfgrad_abort", None),
        ("SCF NOT CONVERGED AFTER 300 CYCLES", "error_scf", "scf_not_converged"),
        ("COULD NOT WRITE TO DISK\n", "error_disk_io", "disk_write_failed"),
        ("", "incomplete", "run_incomplete"),
        ("ORCA FINISHED BY ERROR TERMINATION\n", "unknown_failure", "error_termination"),
    ],
    ids=[
        "multiplicity_impossible",
        "scfgrad_abort",
        "scf_not_converged",
        "disk_io_error",
        "empty_file",
        "generic_error_termination",
    ],
)
def test_opt_failure_verdicts(
    tmp_path: Path, payload: str, expected_status: str, expected_reason: str | None
) -> None:
    result = analyze_output(_write_out(tmp_path, payload), _OPT_MODE)
    assert result.status == expected_status
    if expected_reason is not None:
        assert result.reason == expected_reason


def test_zero_distance_geometry_error(tmp_path: Path) -> None:
    payload = "\n".join(
        [
            "Calculating Nuclear repulsion ... Error (ORCA_GTOINT/SHARK): ",
            "Zero distance encountered between atoms 61 and 20",
            "ORCA finished by error termination in Startup",
        ]
    )
    result = analyze_output(_write_out(tmp_path, payload), _TS_MODE)
    assert result.status == AnalyzerStatus.ERROR_GEOMETRY
    assert result.reason == "geometry_zero_distance"
    assert result.markers["geometry_zero_distance"]
    assert result.markers["generic_error_termination"]


# Non-fatal memory advisories printed verbatim by ORCA 6.1.1
# (liborca_tools ``WARNING - LOW MEMORY`` block, orca_autoci batching warnings).
_ORCA_MEMORY_WARNINGS = (
    "WARNING - LOW MEMORY !!!\n"
    "    CURRENTLY USED: 3900.0 MB\n"
    "    MAXCORE:        4000.0 MB\n"
    "    USING 97.5% OF MAXCORE.\n"
    "INSUFFICIENT MEMORY MAY LEAD TO SLOW PERFORMANCE OR CRASHES.\n",
    "WARNING [Contract_2_4_4_CalcBatching]: Out of memory according to MaxCore limit!\n"
    "   ... Will continue with maximum number of batches anyways.\n",
    "WARNING [Contract_4_4_4_2A_2B_2A_2x_2B_2x_CalcBatching]: Out of memory according to"
    " MaxCore limit!\n",
    "WARNING: Contract_4_6_4__2 Out of memory according to MaxCore limit! \n",
)
_ORCA_MEMORY_WARNING_IDS = ("low_memory", "calc_batching", "calc_batching_2a2b", "contract_4_6_4")


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("warning", _ORCA_MEMORY_WARNINGS, ids=_ORCA_MEMORY_WARNING_IDS)
def test_memory_warning_does_not_fail_a_normally_terminated_run(
    tmp_path: Path, large: bool, warning: str
) -> None:
    filler = "ordinary output\n" * (22000 if large else 1)
    payload = filler + warning + "THE OPTIMIZATION HAS CONVERGED\n" + filler + NORMAL + "\n"

    result = analyze_output(_write_out(tmp_path, payload), _OPT_MODE)

    assert result.status is AnalyzerStatus.COMPLETED
    assert result.reason == "normal_termination"
    assert result.markers["memory_error"] is False


@pytest.mark.parametrize("warning", _ORCA_MEMORY_WARNINGS, ids=_ORCA_MEMORY_WARNING_IDS)
def test_memory_warning_before_an_abort_is_not_a_memory_verdict(
    tmp_path: Path, warning: str
) -> None:
    payload = warning + "ORCA finished by error termination in MDCI\n"

    result = analyze_output(_write_out(tmp_path, payload), _OPT_MODE)

    assert result.status is AnalyzerStatus.UNKNOWN_FAILURE
    assert result.markers["memory_error"] is False


@pytest.mark.parametrize(
    "fatal",
    [
        "ERROR - OUT OF MEMORY !!!\n    MINIMUM REQUIRED:  5000.0 MB\n",
        " OUT OF MEMORY ERROR! \n",
        "!!!                          OUT OF MEMORY                       !!!\n",
        "ERROR (SHARK/DRIVER/SYM): Out of memory!\n",
        "Out of memory! Please increase maxcore!\n",
        "Error (ORCA_MDCI): out of memory in KCcor_MO2\n",
        "Error (DLPNO-MP2): Insufficient memory available!\n",
        "Error (RI-MP2-RESPONSE): insufficient memory (need 1200.0 MB more)\n",
        "cannot allocate memory for cache, abort\n",
    ],
)
def test_orca_memory_abort_is_a_memory_error(tmp_path: Path, fatal: str) -> None:
    payload = fatal + "ORCA finished by error termination in MDCI\n"

    result = analyze_output(_write_out(tmp_path, payload), _OPT_MODE)

    assert result.status is AnalyzerStatus.ERROR_MEMORY
    assert result.reason == "out_of_memory"
    assert result.markers["memory_error"] is True


def test_missing_output_file(tmp_path: Path) -> None:
    result = analyze_output(tmp_path / "nonexistent.out", _OPT_MODE)
    assert result.status == "incomplete"
    assert result.reason == "output_missing"


def test_normal_opt_completed(tmp_path: Path) -> None:
    payload = NORMAL + "\nTOTAL RUN TIME: 0 days 0 hours 5 minutes\n"
    result = analyze_output(_write_out(tmp_path, payload), _OPT_MODE)
    assert result.status == "completed"
    assert result.reason == "normal_termination"
    assert result.markers["total_run_time_seen"]


@pytest.mark.parametrize(
    ("lines", "expected_status", "expected_reason"),
    [
        (
            ["THE OPTIMIZATION DID NOT CONVERGE"],
            AnalyzerStatus.GEOM_NOT_CONVERGED,
            "geometry_not_converged",
        ),
        (
            ["OPTIMIZATION HAS NOT YET CONVERGED", "THE OPTIMIZATION HAS CONVERGED"],
            AnalyzerStatus.COMPLETED,
            "normal_termination",
        ),
        (
            ["THE OPTIMIZATION HAS CONVERGED", "THE OPTIMIZATION DID NOT CONVERGE"],
            AnalyzerStatus.GEOM_NOT_CONVERGED,
            "geometry_not_converged",
        ),
    ],
    ids=[
        "unconverged_is_not_completed",
        "later_convergence_overrides_earlier_warning",
        "later_unconverged_overrides_earlier_convergence",
    ],
)
def test_normal_terminated_opt_convergence_verdict(
    tmp_path: Path, lines: list[str], expected_status: AnalyzerStatus, expected_reason: str
) -> None:
    payload = "\n".join([*lines, NORMAL])
    result = analyze_output(_write_out(tmp_path, payload), _OPT_MODE)
    assert result.status == expected_status
    assert result.reason == expected_reason


def test_not_converged_marker_before_the_tail_window_is_still_a_verdict(tmp_path: Path) -> None:
    # The parser scans the whole file; the analyzer used to read only the last
    # 64 KiB, so a not-converged marker followed by a long normal-modes matrix
    # and a normal termination was reported COMPLETED.
    out_path = tmp_path / "rxn.out"
    filler = "\n".join(f"{i:6d}   0.000000   0.000000   0.000000" for i in range(4000))
    out_path.write_text(
        "! Opt Freq B3LYP def2-SVP\n"
        "THE OPTIMIZATION DID NOT CONVERGE\n"
        "NORMAL MODES\n" + filler + "\n"
        "****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )
    assert out_path.stat().st_size > 64 * 1024

    analysis = analyze_output(out_path, CompletionMode(kind="opt", require_irc=False))

    assert analysis.status is AnalyzerStatus.GEOM_NOT_CONVERGED
    assert analysis.markers["last_opt_converged"] is False


def test_input_block_syntax_abort_is_an_error_termination(tmp_path: Path) -> None:
    # ORCA 6 rejects a malformed input block within a second and prints only
    # ``check syntax!`` / ``LEAVING ORCA``, never an error-termination banner;
    # the analyzer used to report that as ``run_incomplete``.
    out_path = tmp_path / "p11_optts.out"
    out_path.write_text(
        "NOTE: MaxCore=4096 MB was set to SCF,MP2,MDCI,CIPSI,MRCI,RASCI and CIS\n"
        "[file orca_tools/Tool-Scanner/qcscan1.cpp, line 147]: \n"
        "\t Unknown error in GEOM block - check syntax! \n"
        "\t LEAVING ORCA\n"
        "\n"
        "Error in [GEOM] block - Scan_TSMode - Line 16 (TRUST)\n",
        encoding="utf-8",
    )

    analysis = analyze_output(out_path, CompletionMode(kind="ts", require_irc=False))

    assert analysis.status is AnalyzerStatus.UNKNOWN_FAILURE
    assert analysis.reason == "error_termination"
    assert analysis.markers["generic_error_termination"] is True


def test_input_block_syntax_abort_is_error_termination_evidence() -> None:
    text = "\t Unknown error in GEOM block - check syntax! \n\t LEAVING ORCA\n"

    evidence = [termination_line(line) for line in iter_output_lines(text)]
    assert any(error for _normal, error in evidence)
    assert not any(normal for normal, _error in evidence)


# Every frequency-bearing inline output fixture in the test suite, with the
# TS-mode verdict the analyzer gave before the count was consolidated into
# ``frequencies`` (status, imaginary count, final-section flag). The realistic
# ORCA-format texts are shared with the parser and report tests, the short ones
# are the verifier's own fixtures.
_FREQUENCY_FIXTURES: tuple[tuple[str, str, AnalyzerStatus, int, bool], ...] = (
    ("realistic_opt_freq", _B3LYP_OPT_FREQ_COMPLETED, AnalyzerStatus.TS_NOT_FOUND, 0, True),
    ("realistic_ts_imaginary", _TS_OPT_WITH_IMAGINARY, AnalyzerStatus.COMPLETED, 1, True),
    ("realistic_ts_scaling_factor", _TS_REAL_VIB_FORMAT, AnalyzerStatus.COMPLETED, 1, True),
    ("report_ts_block_unterminated", FREQ_TS_BLOCK, AnalyzerStatus.INCOMPLETE, 0, False),
    ("evidence_freqs", _EVIDENCE_FREQUENCIES + NORMAL, AnalyzerStatus.COMPLETED, 1, True),
    (
        "si_ts_with_thermo",
        si_out_text(freqs=(-512.3, 120.0), thermo=True),
        AnalyzerStatus.COMPLETED,
        1,
        True,
    ),
    ("si_noise_mode", si_out_text(freqs=(-5.0, -512.3, 120.0)), AnalyzerStatus.COMPLETED, 1, True),
    ("si_minimum", si_out_text(freqs=(120.0, 300.0)), AnalyzerStatus.TS_NOT_FOUND, 0, True),
    (
        "headerless_legacy_count",
        "some line -123.45 cm**-1\nIRC PATH SUMMARY\n" + NORMAL,
        AnalyzerStatus.COMPLETED,
        1,
        False,
    ),
    (
        "unnumbered_lines",
        "VIBRATIONAL FREQUENCIES\n  -120.00 cm**-1\n  140.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.COMPLETED,
        1,
        True,
    ),
    (
        "last_of_two_sections",
        "VIBRATIONAL FREQUENCIES\n  1   -500.00 cm**-1\n  2   -120.00 cm**-1\n"
        "VIBRATIONAL FREQUENCIES\n  1   -150.00 cm**-1\n  2    120.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.COMPLETED,
        1,
        True,
    ),
    (
        "section_superseded_by_final_energy",
        "VIBRATIONAL FREQUENCIES\n  1   -650.00 cm**-1\n  2    120.00 cm**-1\n"
        "FINAL SINGLE POINT ENERGY      -100.100000000000\n"
        "THE OPTIMIZATION HAS CONVERGED\n"
        "FINAL SINGLE POINT ENERGY      -100.200000000000\n" + NORMAL,
        AnalyzerStatus.TS_NOT_FOUND,
        0,
        False,
    ),
    (
        "recalc_hess_sections",
        "VIBRATIONAL FREQUENCIES\n  1   -650.00 cm**-1\n  2   -120.00 cm**-1\n"
        "FINAL SINGLE POINT ENERGY      -100.100000000000\n"
        "VIBRATIONAL FREQUENCIES\n  1   -600.00 cm**-1\n  2   -110.00 cm**-1\n"
        "FINAL SINGLE POINT ENERGY      -100.200000000000\n"
        "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n  2    120.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.COMPLETED,
        1,
        True,
    ),
    (
        "two_modes",
        "FINAL SINGLE POINT ENERGY      -100.200000000000\n"
        "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n  2   -120.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.TS_NOT_FOUND,
        2,
        True,
    ),
    (
        "geometry_failure_keeps_count",
        "THE OPTIMIZATION DID NOT CONVERGE\nFINAL SINGLE POINT ENERGY      -100.2\n"
        "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.GEOM_NOT_CONVERGED,
        1,
        False,
    ),
    (
        "scf_failure_keeps_count",
        "SCF NOT CONVERGED\nFINAL SINGLE POINT ENERGY      -100.2\n"
        "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.ERROR_SCF,
        1,
        False,
    ),
    (
        "form_feed_inside_section",
        "FINAL SINGLE POINT ENERGY      -100.200000000000\n"
        "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n  2    120.00 cm**-1\n"
        "restart log\x0cFINAL SINGLE POINT ENERGY      -100.300000000000\n" + NORMAL,
        AnalyzerStatus.COMPLETED,
        1,
        True,
    ),
    (
        "tiny_negative_is_noise",
        "VIBRATIONAL FREQUENCIES\n  1     -5.00 cm**-1\n  2   -450.00 cm**-1\n"
        "  3    120.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.COMPLETED,
        1,
        True,
    ),
    ("no_frequencies", NORMAL + "\nTOTAL RUN TIME: 0 days", AnalyzerStatus.TS_NOT_FOUND, 0, False),
    (
        "echoed_section_is_not_evidence",
        "| 1> # IRC PATH SUMMARY\n| 2> # VIBRATIONAL FREQUENCIES -150.0 cm**-1\n"
        "ordinary output\n" + NORMAL,
        AnalyzerStatus.TS_NOT_FOUND,
        0,
        False,
    ),
    (
        "no_imaginary_mode",
        "VIBRATIONAL FREQUENCIES\n  1    120.00 cm**-1\n  2    240.00 cm**-1\n" + NORMAL,
        AnalyzerStatus.TS_NOT_FOUND,
        0,
        True,
    ),
)


@pytest.mark.parametrize(
    ("text", "status", "count", "final_section"),
    [fixture[1:] for fixture in _FREQUENCY_FIXTURES],
    ids=[fixture[0] for fixture in _FREQUENCY_FIXTURES],
)
def test_verifier_count_is_the_published_frequency_analysis(
    tmp_path: Path,
    text: str,
    status: AnalyzerStatus,
    count: int,
    final_section: bool,
) -> None:
    # The TS verdict and the SI/report frequency analysis must count the same
    # modes of the same section; the expectations pin the verdicts from before
    # the consolidation.
    out = tmp_path / "rxn.out"
    out.write_text(text, encoding="utf-8")

    result = analyze_output(out, _TS_FREQ_MODE)
    analysis = parsed_frequency_analysis(out)

    assert result.status is status
    assert result.markers["imaginary_frequency_count"] == count
    assert result.markers["final_frequency_section"] is final_section
    if not result.markers["terminated_normally"]:
        # An unterminated run is never counted; nothing is published for it.
        assert count == 0
    elif analysis is not None:
        assert analysis.imaginary_count() == count
    else:
        assert not final_section


def test_utf16_output_gets_the_same_verdict_as_its_utf8_twin(tmp_path: Path) -> None:
    # ORCA can write UTF-16 output; the analyzer used to decode every file as
    # UTF-8 and so saw NUL-riddled text where the parser saw a normal run.
    utf8 = tmp_path / "utf8.out"
    utf8.write_text(_TS_REAL_VIB_FORMAT, encoding="utf-8")
    utf16 = tmp_path / "utf16.out"
    utf16.write_text(_TS_REAL_VIB_FORMAT, encoding="utf-16")
    assert utf16.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff"))

    expected = analyze_output(utf8, _TS_FREQ_MODE)
    result = analyze_output(utf16, _TS_FREQ_MODE)

    assert expected.status is AnalyzerStatus.COMPLETED
    assert result.status is AnalyzerStatus.COMPLETED
    assert result.reason == expected.reason == "ts_criteria_met"
    assert {**result.markers, "out_path": ""} == {**expected.markers, "out_path": ""}
    assert result.markers["imaginary_frequency_count"] == 1


def _analyze(tmp_path: Path, text: str) -> AnalyzerStatus:
    out = tmp_path / "calc.out"
    out.write_text(text, encoding="utf-8")
    result = analyze_output(out, CompletionMode(kind="opt", require_irc=False))
    return result.status


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("OUT OF MEMORY\n", id="out_of_memory"),
        pytest.param("INSUFFICIENT MEMORY\n", id="insufficient_memory"),
        pytest.param("CANNOT ALLOCATE MEMORY\n", id="cannot_allocate_memory"),
        pytest.param("OUT OF MEMORY\nSCF NOT CONVERGED\n", id="memory_takes_priority_over_scf"),
    ],
)
def test_memory_error_detection(tmp_path: Path, text: str) -> None:
    assert _analyze(tmp_path, text) == AnalyzerStatus.ERROR_MEMORY


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("THE OPTIMIZATION DID NOT CONVERGE\n", id="did_not_converge"),
        pytest.param("OPTIMIZATION HAS NOT YET CONVERGED\n", id="not_yet_converged"),
        pytest.param(
            "OPTIMIZATION HAS NOT YET CONVERGED\n****ORCA TERMINATED NORMALLY****\n",
            id="terminated_normally_still_failed",
        ),
    ],
)
def test_geom_not_converged_detection(tmp_path: Path, text: str) -> None:
    assert _analyze(tmp_path, text) == AnalyzerStatus.GEOM_NOT_CONVERGED


def test_apply_exit_code_never_publishes_success_over_a_failed_process(tmp_path: Path) -> None:
    out = _write_out(
        tmp_path,
        "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n  2    120.00 cm**-1\n"
        "****ORCA TERMINATED NORMALLY****\n",
    )
    completed = analyze_output(out, _TS_MODE)
    assert completed.status == AnalyzerStatus.COMPLETED
    assert completed.markers["final_frequency_section"] is True

    assert apply_exit_code(completed, 0) is completed
    rejected = apply_exit_code(completed, 1)
    assert (rejected.status, rejected.reason) == (
        AnalyzerStatus.UNKNOWN_FAILURE,
        "nonzero_exit_code",
    )
    assert rejected.markers["final_frequency_section"] is False
    assert rejected.markers["imaginary_frequency_count"] == 1
    assert completed.markers["final_frequency_section"] is True

    failed = OutAnalysis(AnalyzerStatus.ERROR_SCF, "scf_not_converged", completed.markers)
    assert apply_exit_code(failed, 42) is failed
