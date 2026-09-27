from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

from orca_auto.orca.attempt.reporting import decide_attempt_outcome
from orca_auto.orca.attempt.run import run_attempt
from orca_auto.orca.input_syntax import orca_route_tokens
from orca_auto.orca.input_validation import validate_supported_xyz_geometry_syntax
from orca_auto.orca.orca_runner import RunResult
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state

Attempt = Callable[..., int]


def _summary_lines(out: str, label: str) -> list[str]:
    return [line.split(": ", 1)[1] for line in out.splitlines() if line.startswith(f"{label}: ")]


@pytest.mark.parametrize("keyword", ["ScanTS", "scants", "SCANTS"])
def test_direct_scants_is_rejected(keyword: str) -> None:
    with pytest.raises(ValueError, match="unsupported.*ScanTS"):
        validate_supported_xyz_geometry_syntax(
            [f"! B3LYP {keyword} Freq", "* xyz 0 1", "H 0 0 0", "H 0 0 0.74", "*"],
            label="ORCA selected input",
        )


@pytest.mark.parametrize("route", ["! r2SCAN-3c Opt", "! Opt # ScanTS", "# ! ScanTS"])
def test_scants_comments_and_scan_functionals_are_not_rejected(route: str) -> None:
    lines = [route, "* xyz 0 1", "H 0 0 0", "H 0 0 0.74", "*"]

    # Accepted: validation completes without raising ...
    validate_supported_xyz_geometry_syntax(lines, label="input")
    # ... because neither a comment nor a scan functional yields a ScanTS route token.
    assert "scants" not in {token.value.lower() for token in orca_route_tokens(route)}


def test_calculation_api_has_no_retry_policy(tmp_path: Path) -> None:
    for function in (new_state, run_attempt, decide_attempt_outcome):
        assert not any("retry" in name for name in inspect.signature(function).parameters)
    assert "max_retries" not in new_state(tmp_path, tmp_path / "calc.inp")


@pytest.mark.parametrize(
    "output",
    [
        "SCF NOT CONVERGED",
        "OUT OF MEMORY",
        "COULD NOT WRITE TO DISK",
        "THE OPTIMIZATION DID NOT CONVERGE",
        "ZERO DISTANCE ENCOUNTERED",
        "ORCA finished by error termination in Startup",
        "NO ACCEPTABLE TS",
        "incomplete output",
    ],
)
def test_calculation_failure_runs_once_with_original_reason(
    tmp_path: Path, output: str, attempt: Attempt, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = tmp_path / "calc.inp"
    selected.write_text("! OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    seen: list[Path] = []

    def run(path: Path) -> RunResult:
        seen.append(path)
        path.with_suffix(".gbw").write_bytes(b"intact-checkpoint")
        path.with_suffix(".xyz").write_text("2\ngeometry\nH 0 0 0\nH 0 0 0.75\n")
        out = path.with_suffix(".out")
        out.write_text(output)
        return RunResult(out_path=str(out), return_code=1)

    assert attempt(selected, run) == 1
    saved = load_state(tmp_path)
    assert saved is not None
    assert seen == [selected]
    assert len(saved["attempts"]) == 1
    final_result = saved["final_result"]
    assert final_result is not None
    assert final_result["reason"] == saved["attempts"][0]["analyzer_reason"]
    assert "max_retries" not in saved
    assert list(tmp_path.glob("*.inp")) == [selected]
    assert len(_summary_lines(capsys.readouterr().out, "status")) == 1


@pytest.mark.parametrize("return_code", [1, -15, 42])
@pytest.mark.parametrize("route", ["! SP", "! OptTS Freq"])
def test_nonzero_exit_rejects_otherwise_completed_attempt(
    tmp_path: Path,
    return_code: int,
    route: str,
    attempt: Attempt,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = tmp_path / "calc.inp"
    selected.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    seen: list[Path] = []

    def run(path: Path) -> RunResult:
        seen.append(path)
        out = path.with_suffix(".out")
        out.write_text(
            "FINAL SINGLE POINT ENERGY -1.1\n"
            "THE OPTIMIZATION HAS CONVERGED\n"
            "VIBRATIONAL FREQUENCIES\n"
            "  1   -420.00 cm**-1\n"
            "  2    120.00 cm**-1\n"
            "****ORCA TERMINATED NORMALLY****\n"
        )
        return RunResult(out_path=str(out), return_code=return_code)

    assert attempt(selected, run) == 1
    saved = load_state(tmp_path)
    assert saved is not None
    assert saved["status"] == "failed"
    assert seen == [selected]
    assert len(saved["attempts"]) == 1
    record = saved["attempts"][0]
    assert record["return_code"] == return_code
    assert record["analyzer_status"] == "unknown_failure"
    assert record["analyzer_reason"] == "nonzero_exit_code"
    assert record["markers"]["terminated_normally"] is True
    assert record["markers"]["final_frequency_section"] is False
    if "OptTS" in route:
        assert record["markers"]["imaginary_frequency_count"] == 1
    assert saved["final_result"] is not None
    assert saved["final_result"]["status"] == "failed"
    assert saved["final_result"]["reason"] == "nonzero_exit_code"
    out = capsys.readouterr().out
    assert _summary_lines(out, "status") == ["failed"]
    assert _summary_lines(out, "reason") == ["nonzero_exit_code"]
    assert _summary_lines(out, "attempt_count") == ["1"]


@pytest.mark.parametrize(
    ("route", "failure_line", "reason"),
    [
        ("! SP", "SCF NOT CONVERGED", "scf_not_converged"),
        ("! SP", "OUT OF MEMORY", "out_of_memory"),
        ("! SP", "COULD NOT WRITE TO DISK", "disk_write_failed"),
        ("! Opt", "THE OPTIMIZATION DID NOT CONVERGE", "geometry_not_converged"),
        ("! SP", "ORCA FINISHED BY ERROR TERMINATION", "error_termination"),
        (
            "! OptTS Freq",
            "VIBRATIONAL FREQUENCIES\n  1   -420.00 cm**-1\n  2   -120.00 cm**-1",
            "ts_criteria_failed",
        ),
    ],
)
def test_nonzero_exit_preserves_specific_failure_with_normal_marker(
    tmp_path: Path,
    route: str,
    failure_line: str,
    reason: str,
    attempt: Attempt,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = tmp_path / "calc.inp"
    selected.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    seen: list[Path] = []

    def run(path: Path) -> RunResult:
        seen.append(path)
        out = path.with_suffix(".out")
        out.write_text(f"{failure_line}\n****ORCA TERMINATED NORMALLY****\n")
        return RunResult(out_path=str(out), return_code=42)

    assert attempt(selected, run) == 1
    saved = load_state(tmp_path)
    assert saved is not None and saved["status"] == "failed"
    assert seen == [selected]
    assert len(saved["attempts"]) == 1
    assert saved["attempts"][0]["return_code"] == 42
    assert saved["attempts"][0]["analyzer_reason"] == reason
    if reason == "ts_criteria_failed":
        assert saved["attempts"][0]["markers"]["imaginary_frequency_count"] == 2
        assert saved["attempts"][0]["markers"]["final_frequency_section"] is True
    assert saved["final_result"] is not None
    assert saved["final_result"]["reason"] == reason
    assert _summary_lines(capsys.readouterr().out, "reason") == [reason]
