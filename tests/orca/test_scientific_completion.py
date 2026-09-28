from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import detect_completion_mode
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.statuses import AnalyzerStatus


@pytest.mark.parametrize(
    ("route", "body", "status", "reason"),
    [
        (
            "! HF STO-3G Opt",
            "FINAL SINGLE POINT ENERGY -1.0\n",
            AnalyzerStatus.INCOMPLETE,
            "optimization_evidence_missing",
        ),
        ("! HF STO-3G", "", AnalyzerStatus.INCOMPLETE, "energy_evidence_missing"),
        (
            "! HF STO-3G Opt Freq",
            "FINAL SINGLE POINT ENERGY -1.0\nTHE OPTIMIZATION HAS CONVERGED\n",
            AnalyzerStatus.INCOMPLETE,
            "frequency_evidence_missing",
        ),
        (
            "! HF STO-3G Opt",
            "SCF NOT CONVERGED\nSCF CONVERGED AFTER 12 CYCLES\n"
            "FINAL SINGLE POINT ENERGY -1.0\nTHE OPTIMIZATION HAS CONVERGED\n",
            AnalyzerStatus.COMPLETED,
            "normal_termination",
        ),
    ],
)
def test_completion_requires_requested_final_evidence(
    tmp_path: Path,
    route: str,
    body: str,
    status: AnalyzerStatus,
    reason: str,
) -> None:
    inp = tmp_path / "job.inp"
    inp.write_text(route + "\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    out = inp.with_suffix(".out")
    out.write_text(body + "****ORCA TERMINATED NORMALLY****\n")
    result = analyze_output(out, detect_completion_mode(inp))
    assert (result.status, result.reason) == (status, reason)
