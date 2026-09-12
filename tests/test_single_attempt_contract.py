from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.orca.attempt.engine import run_attempts
from orca_auto.orca.input_blocks import validate_supported_xyz_geometry_syntax
from orca_auto.orca.state import new_state
from orca_auto.orca.state_machine import decide_attempt_outcome
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.types import RunFinishedNotification


@pytest.mark.parametrize("keyword", ["ScanTS", "scants", "SCANTS"])
def test_direct_scants_is_rejected(keyword: str) -> None:
    with pytest.raises(ValueError, match="unsupported.*ScanTS"):
        validate_supported_xyz_geometry_syntax(
            [f"! B3LYP {keyword} Freq", "* xyz 0 1", "H 0 0 0", "H 0 0 0.74", "*"],
            label="ORCA selected input",
        )


@pytest.mark.parametrize("route", ["! r2SCAN-3c Opt", "! Opt # ScanTS", "# ! ScanTS"])
def test_scants_comments_and_scan_functionals_are_not_rejected(route: str) -> None:
    validate_supported_xyz_geometry_syntax(
        [route, "* xyz 0 1", "H 0 0 0", "H 0 0 0.74", "*"], label="input"
    )


def test_calculation_api_has_no_retry_policy(tmp_path: Path) -> None:
    for function in (new_state, run_attempts, decide_attempt_outcome):
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
def test_calculation_failure_runs_once_with_original_reason(tmp_path: Path, output: str) -> None:
    selected = tmp_path / "calc.inp"
    selected.write_text("! OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    seen: list[Path] = []
    events: list[dict] = []

    class Runner:
        def run(self, path: Path):
            seen.append(path)
            path.with_suffix(".gbw").write_bytes(b"intact-checkpoint")
            path.with_suffix(".xyz").write_text("2\ngeometry\nH 0 0 0\nH 0 0 0.75\n")
            out = path.with_suffix(".out")
            out.write_text(output)
            return SimpleNamespace(out_path=str(out), return_code=1)

    assert (
        run_attempts(
            tmp_path,
            selected,
            new_state(tmp_path, selected),
            resumed=False,
            runner=Runner(),
            emit=events.append,
        )
        == 1
    )
    saved = load_state(tmp_path)
    assert saved is not None
    assert seen == [selected]
    assert len(saved["attempts"]) == 1
    final_result = saved["final_result"]
    assert final_result is not None
    assert final_result["reason"] == saved["attempts"][0]["analyzer_reason"]
    assert "max_retries" not in saved
    assert list(tmp_path.glob("*.inp")) == [selected]
    assert len(events) == 1


@pytest.mark.parametrize("return_code", [1, -15, 42])
@pytest.mark.parametrize("route", ["! SP", "! OptTS Freq"])
def test_nonzero_exit_rejects_otherwise_completed_attempt(
    tmp_path: Path, return_code: int, route: str
) -> None:
    selected = tmp_path / "calc.inp"
    selected.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    seen: list[Path] = []
    events: list[dict] = []
    notifications: list[RunFinishedNotification] = []

    class Runner:
        def run(self, path: Path):
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
            return SimpleNamespace(out_path=str(out), return_code=return_code)

    assert (
        run_attempts(
            tmp_path,
            selected,
            new_state(tmp_path, selected),
            resumed=False,
            runner=Runner(),
            emit=events.append,
            notify_finished=notifications.append,
        )
        == 1
    )
    saved = load_state(tmp_path)
    assert saved is not None
    assert saved["status"] == "failed"
    assert seen == [selected]
    assert len(saved["attempts"]) == 1
    attempt = saved["attempts"][0]
    assert attempt["return_code"] == return_code
    assert attempt["analyzer_status"] == "unknown_failure"
    assert attempt["analyzer_reason"] == "nonzero_exit_code"
    assert attempt["markers"]["terminated_normally"] is True
    assert attempt["markers"]["final_frequency_section"] is False
    if "OptTS" in route:
        assert attempt["markers"]["imaginary_frequency_count"] == 1
    assert saved["final_result"] is not None
    assert saved["final_result"]["status"] == "failed"
    assert saved["final_result"]["reason"] == "nonzero_exit_code"
    assert len(events) == 1 and events[0]["status"] == "failed"
    assert len(notifications) == 1
    assert notifications[0]["status"] == "failed"
    assert notifications[0]["reason"] == "nonzero_exit_code"
    assert notifications[0]["attempt_count"] == 1


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
    tmp_path: Path, route: str, failure_line: str, reason: str
) -> None:
    selected = tmp_path / "calc.inp"
    selected.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    seen: list[Path] = []
    events: list[dict] = []

    class Runner:
        def run(self, path: Path):
            seen.append(path)
            out = path.with_suffix(".out")
            out.write_text(f"{failure_line}\n****ORCA TERMINATED NORMALLY****\n")
            return SimpleNamespace(out_path=str(out), return_code=42)

    assert (
        run_attempts(
            tmp_path,
            selected,
            new_state(tmp_path, selected),
            resumed=False,
            runner=Runner(),
            emit=events.append,
        )
        == 1
    )
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
    assert len(events) == 1 and events[0]["reason"] == reason
