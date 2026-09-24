from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.output_status import has_error_termination, has_normal_termination
from orca_auto.orca.parser import parse_orca_output

NORMAL = "****ORCA TERMINATED NORMALLY****"
ERROR = "ORCA FINISHED BY ERROR TERMINATION in Startup"


@pytest.mark.parametrize("kind", ["opt", "ts"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("error_first", [False, True])
def test_terminal_error_blocks_normal_completion(
    tmp_path: Path, kind: str, newline: str, large: bool, error_first: bool
) -> None:
    # Keep the error outside both the head and tail windows in the large case.
    filler = "irrelevant output\n" * (20000 if large else 1)
    lines = [ERROR, NORMAL] if error_first else [NORMAL, ERROR]
    text = (filler + lines[0] + "\n" + filler + lines[1] + "\n" + filler).replace("\n", newline)
    out = tmp_path / "conflict.out"
    out.write_bytes(text.encode())
    mode = CompletionMode("ts" if kind == "ts" else "opt", False, "! OptTS Freq")
    result = analyze_output(out, mode)
    assert result.status == "unknown_failure"
    assert result.reason == "error_termination"
    assert result.markers["terminated_normally"] is True
    assert result.markers["generic_error_termination"] is True
    assert result.markers["final_frequency_section"] is False


@pytest.mark.parametrize("prefix", ["# ", "  # ", "|  27> # ", "  | 2> "])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_quoted_termination_markers_are_not_execution_evidence(
    tmp_path: Path, prefix: str, newline: str
) -> None:
    text = newline.join([prefix + ERROR, prefix + "FATAL ERROR: check syntax", prefix + NORMAL])
    assert not has_normal_termination(text)
    assert not has_error_termination(text)
    out = tmp_path / "echo.out"
    out.write_bytes(text.encode())
    mode = CompletionMode("opt", False, "! SP")
    assert analyze_output(out, mode).status == "incomplete"
    out.write_bytes((text + newline + NORMAL + newline).encode())
    assert analyze_output(out, mode).status == "completed"


@pytest.mark.parametrize("quoted", [ERROR, NORMAL])
@pytest.mark.parametrize("kind", ["opt", "ts"])
def test_tail_cut_inside_input_echo_does_not_create_termination_evidence(
    tmp_path: Path, quoted: str, kind: str
) -> None:
    # The tail starts inside this echo, without its identifying line prefix.
    text = "|  1> # " + "x" * 300000 + quoted + "\n"
    if quoted == ERROR:
        text += "VIBRATIONAL FREQUENCIES\n -150.0 cm**-1\n" + NORMAL + "\n"
    out = tmp_path / "long_echo.out"
    out.write_text(text)
    result = analyze_output(
        out, CompletionMode("ts" if kind == "ts" else "opt", False, "! OptTS Freq")
    )
    assert result.markers["generic_error_termination"] is False
    assert result.status == ("completed" if quoted == ERROR else "incomplete")


@pytest.mark.parametrize(
    "diagnostic",
    [
        "**** FATAL ERROR ENCOUNTERED ****",
        ".... aborting the run",
        "ORCA has ended prematurely and may have crashed",
        "LEAVING ORCA",
        "Error in GEOM block - check syntax!",
    ],
)
def test_existing_terminal_diagnostics_keep_their_failure_meaning(
    tmp_path: Path, diagnostic: str
) -> None:
    assert has_error_termination(diagnostic)
    out = tmp_path / "diagnostic.out"
    out.write_text(diagnostic + "\n" + NORMAL + "\n")
    result = analyze_output(out, CompletionMode("opt", False, "! SP"))
    assert result.status == "unknown_failure"
    assert result.reason == "error_termination"


def test_specific_ts_failure_reason_survives_generic_termination(tmp_path: Path) -> None:
    out = tmp_path / "ts.out"
    out.write_text("NO ACCEPTABLE TS\n" + ERROR + "\n")
    result = analyze_output(out, CompletionMode("ts", False, "! OptTS"))
    assert result.status == "ts_not_found"
    assert result.reason == "ts_failure_marker"


@pytest.mark.parametrize("quoted", ["NO ACCEPTABLE TS", "FAILED TO FIND TS"])
def test_termination_fix_does_not_promote_quoted_ts_failure(tmp_path: Path, quoted: str) -> None:
    out = tmp_path / "normal_ts.out"
    out.write_text(
        "| 1> # " + quoted + "\nVIBRATIONAL FREQUENCIES\n -150.0 cm**-1\n" + NORMAL + "\n"
    )
    result = analyze_output(out, CompletionMode("ts", False, "! OptTS Freq"))
    assert result.status == "completed"
    assert result.reason == "ts_criteria_met"


@pytest.mark.parametrize("prefix", ["# ", "|  2> # ", "  | 2> "])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "SCF NOT CONVERGED",
        "OUT OF MEMORY",
        "NO SPACE LEFT ON DEVICE",
        "ZERO DISTANCE BETWEEN ATOMS",
        "MULTIPLICITY IMPOSSIBLE",
        "THE OPTIMIZATION DID NOT CONVERGE",
    ],
)
def test_quoted_diagnostics_do_not_fail_successful_execution(
    tmp_path: Path, prefix: str, large: bool, diagnostic: str
) -> None:
    out = tmp_path / "commented_diagnostic.out"
    filler = "ordinary output\n" * (22000 if large else 1)
    out.write_text(prefix + diagnostic + "\n" + filler + NORMAL + "\n")

    result = analyze_output(out, CompletionMode("opt", False, "! SP"))

    assert result.status == "completed"
    assert result.markers["last_opt_converged"] is None
    assert parse_orca_output(str(out)).opt_converged is None


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize(
    "diagnostic,status",
    [
        ("SCF NOT CONVERGED", "error_scf"),
        ("OUT OF MEMORY", "error_memory"),
        ("NO SPACE LEFT ON DEVICE", "error_disk_io"),
        ("ZERO DISTANCE BETWEEN ATOMS", "error_geometry"),
        ("MULTIPLICITY IMPOSSIBLE", "error_multiplicity_impossible"),
    ],
)
def test_actual_diagnostic_verdict_does_not_depend_on_output_size(
    tmp_path: Path, large: bool, diagnostic: str, status: str
) -> None:
    # The actual diagnostic lies outside both former sampling windows.
    filler = "ordinary output\n" * (22000 if large else 1)
    out = tmp_path / "actual_diagnostic.out"
    out.write_text(filler + diagnostic + "\n" + filler + NORMAL + "\n")

    assert analyze_output(out, CompletionMode("opt", False, "! SP")).status == status


@pytest.mark.parametrize("diagnostic", ["SCF NOT CONVERGED", "OUT OF MEMORY"])
def test_partial_tail_of_long_input_echo_is_not_diagnostic_evidence(
    tmp_path: Path, diagnostic: str
) -> None:
    out = tmp_path / "long_diagnostic_echo.out"
    out.write_text("| 1> # " + "x" * 300000 + diagnostic + "\n" + NORMAL + "\n")

    assert analyze_output(out, CompletionMode("opt", False, "! SP")).status == "completed"


@pytest.mark.parametrize("large", [False, True])
def test_ts_verification_ignores_echoed_frequency_and_irc_evidence(
    tmp_path: Path, large: bool
) -> None:
    out = tmp_path / "quoted_ts_verification.out"
    filler = "ordinary output\n" * (22000 if large else 1)
    out.write_text(
        "| 1> # IRC PATH SUMMARY\n"
        "| 2> # VIBRATIONAL FREQUENCIES -150.0 cm**-1\n" + filler + NORMAL + "\n"
    )

    result = analyze_output(out, CompletionMode("ts", True, "! OptTS Freq IRC"))

    assert result.status == "ts_not_found"
    assert result.markers["imaginary_frequency_count"] == 0
    assert result.markers["irc_marker_found"] is False
