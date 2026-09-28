"""Replay unedited bounded ORCA 6.1.1 outputs; no engine installation required."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import detect_completion_mode
from orca_auto.orca.evidence import parsed_final_output
from orca_auto.orca.out_analyzer import analyze_output

FIXTURES = Path(__file__).parents[1] / "fixtures" / "orca_6_1_1"


@pytest.mark.parametrize(
    ("case", "status", "energy", "optimization", "modes", "imaginary"),
    [
        ("h2_sp", "completed", -1.116759307204, None, None, None),
        ("water_opt_freq", "completed", -74.965901189921, True, 9, 0),
        ("water_not_converged", "geom_not_converged", -74.941321118008, False, None, None),
        ("nh3_ts_irc", "completed", -55.437665301787, True, 12, 1),
    ],
)
def test_authentic_completion_and_published_evidence(
    case: str,
    status: str,
    energy: float,
    optimization: bool | None,
    modes: int | None,
    imaginary: int | None,
) -> None:
    manifest = json.loads((FIXTURES / "provenance.json").read_text())
    for filename, recorded in manifest["cases"][case]["files"].items():
        data = (FIXTURES / filename).read_bytes()
        assert hashlib.sha256(data).hexdigest() == recorded["sha256"]
        assert len(data) == recorded["bytes"]
    output = FIXTURES / f"{case}.out"
    verdict = analyze_output(output, detect_completion_mode(FIXTURES / f"{case}.inp"))
    parsed, frequencies = parsed_final_output(output)

    assert verdict.status == status
    assert parsed.energy_hartree == pytest.approx(energy, abs=1e-10)
    assert verdict.markers["energy_hartree"] == pytest.approx(energy, abs=1e-10)
    assert parsed.opt_converged is optimization
    assert verdict.markers["last_opt_converged"] is optimization
    if modes is None:
        assert frequencies is None
        assert verdict.markers["final_frequency_section"] is False
    else:
        assert frequencies is not None
        assert len(frequencies.frequencies) == modes
        assert frequencies.imaginary_count() == imaginary
        assert verdict.markers["imaginary_frequency_count"] == imaginary
        assert verdict.markers["final_frequency_section"] is True
