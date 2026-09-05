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
