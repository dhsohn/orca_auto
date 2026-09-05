"""Single-point job report (job_report.html for SP / bare-Freq jobs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import write_job_html_report
from orca_auto.orca.report.sp import collect_sp_report_data
from orca_auto.orca.state import write_report_files
from tests.engine_artifact_helpers import bind_report_generation, report_generation_target


def _out_text(
    *,
    route: str = "wB97M-V def2-TZVPP CPCM(toluene)",
    energy: float = -1234.567890123456,
    freq_block: bool = False,
    thermo: bool = False,
) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {route}",
        "|  2> * xyz 0 1",
        "|  3> C 0.0 0.0 0.0",
        "|  4> *",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
        "  C      0.000000    1.234567   -0.987654",
        "  H      0.123456   -0.654321    2.000000",
        "",
        f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
    ]
    if freq_block:
        lines += ["", "VIBRATIONAL FREQUENCIES", "-----------------------", ""]
        lines += ["     0:      -80.50 cm**-1 ***imaginary mode***", "     1:      120.00 cm**-1"]
        lines += [
            "",
            "NORMAL MODES",
            "------------",
            "",
            "                  0          1",
            "      0       0.700000   0.100000",
            "      1       0.100000   0.000000",
            "      2       0.000000   0.000000",
            "      3       0.500000   0.000000",
            "      4       0.000000   0.200000",
            "      5       0.000000   0.100000",
        ]
    if thermo:
        lines += [
            "--------------------------",
            "THERMOCHEMISTRY AT 298.15K",
            "--------------------------",
            "Zero point energy                ...      0.08843782 Eh",
            "Total enthalpy                   ...  -1234.40000000 Eh",
            "Final Gibbs free energy          ...  -1234.45000000 Eh",
            "G-E(el)                          ...      0.11789012 Eh",
        ]
    lines += [
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)


def _job_dir(
    tmp_path: Path,
    *,
    inp_text: str,
    out_text: str,
    status: str = "completed",
) -> dict[str, Any]:
    inp = tmp_path / "rxn.inp"
    inp.write_text(inp_text, encoding="utf-8")
    out = tmp_path / "rxn.out"
    out.write_text(out_text, encoding="utf-8")
    return {
        "job_id": "job_test",
        "run_id": "run_test",
        "reaction_dir": str(tmp_path),
        "selected_inp": str(inp),
        "status": status,
        "started_at": "2026-07-06T01:00:00+00:00",
        "updated_at": "2026-07-06T02:00:00+00:00",
        "attempts": [
            {
                "index": 1,
                "inp_path": str(inp),
                "out_path": str(out),
                "return_code": 0,
                "analyzer_status": status,
                "analyzer_reason": "normal_termination",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-06T01:00:00+00:00",
                "ended_at": "2026-07-06T01:20:00+00:00",
            }
        ],
        "final_result": {
            "status": status,
            "analyzer_status": status,
            "reason": "normal_termination",
            "completed_at": "2026-07-06T01:20:30+00:00",
            "last_out_path": str(out),
        },
    }


_SP_INP = "! wB97M-V def2-TZVPP CPCM(toluene)\n* xyz 0 1\nC 0 0 0\n*\n"
_FREQ_INP = "! B3LYP def2-SVP Freq\n* xyz 0 1\nC 0 0 0\n*\n"


def test_collect_sp_report_parses_energy_and_si_block(tmp_path: Path) -> None:
    state = _job_dir(tmp_path, inp_text=_SP_INP, out_text=_out_text())

    data = collect_sp_report_data(tmp_path, state)

    assert data is not None
    assert data.result is not None
    assert data.result.energy_hartree == pytest.approx(-1234.567890123456)
    assert data.imaginary_count is None
    assert data.mode_summaries == ()
    assert data.si_block_text is not None
    assert data.si_block_text.startswith(f"== {tmp_path.name} ==")


def test_sp_report_html_renders_energy_and_embedded_si_block(tmp_path: Path) -> None:
    state = _job_dir(tmp_path, inp_text=_SP_INP, out_text=_out_text())

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "SP report" in text
    assert "-1234.567890" in text
    assert "Charge / multiplicity" in text
    assert "initial SP" in text
    # Pure single point: no frequency data, so no vibrational section.
    assert "Vibrational summary" not in text
    assert "Imaginary frequencies" not in text
    # Copy-paste-ready SI block embedded in the page.
    assert "<pre>" in text
    assert f"== {tmp_path.name} ==" in text
    assert "si_block.md" in text


def test_bare_freq_report_includes_vibrational_summary_and_thermo(tmp_path: Path) -> None:
    state = _job_dir(
        tmp_path,
        inp_text=_FREQ_INP,
        out_text=_out_text(route="B3LYP def2-SVP Freq", freq_block=True, thermo=True),
    )

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "SP report" in text
    assert "Vibrational summary" in text
    assert "Imaginary frequencies" in text
    assert "-80.5" in text
    assert "ZPE correction" in text
    assert "G-E(el)" in text


def test_failed_sp_report_keeps_attempt_chain_without_si_block(tmp_path: Path) -> None:
    state = _job_dir(tmp_path, inp_text=_SP_INP, out_text=_out_text(), status="failed")

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Attempt chain" in text
    # No SI block for an incomplete job: nothing must look publishable.
    assert "SI block" not in text


def test_write_report_files_emits_html_and_si_for_sp(tmp_path: Path) -> None:
    state = _job_dir(tmp_path, inp_text=_SP_INP, out_text=_out_text())
    generation = bind_report_generation(tmp_path, state)

    reports = write_report_files(tmp_path, state)

    assert reports["report_html"] == str(generation / "job_report.html")
    assert reports["si_block"] == str(generation / "si_block.md")
    assert (generation / "job_report.html").exists()
    assert (generation / "si_block.md").exists()


def test_vibrational_summary_prefers_the_final_output(tmp_path: Path) -> None:
    # The metric cards must agree with the embedded SI block, which is built
    # from the final output only; the attempt chain is just the fallback.
    state = _job_dir(
        tmp_path,
        inp_text=_FREQ_INP,
        out_text=_out_text(route="B3LYP def2-SVP Freq", freq_block=True, thermo=True),
    )
    stale = tmp_path / "rxn_stale.out"
    stale.write_text(
        _out_text(route="B3LYP def2-SVP Freq", freq_block=True).replace("-80.50", "-333.00"),
        encoding="utf-8",
    )
    # Last attempt points at a stale output; final_result still names rxn.out.
    state["attempts"][-1]["out_path"] = str(stale)

    data = collect_sp_report_data(tmp_path, state)

    assert data is not None
    assert data.imaginary_count == 1
    assert any(abs(summary.frequency_cm + 80.5) < 0.01 for summary in data.mode_summaries)


def test_vibrational_summary_falls_back_to_attempt_outputs(tmp_path: Path) -> None:
    # Final output without a frequency section (e.g. the freq step never ran):
    # an earlier attempt's analysis still fills the vibrational summary.
    state = _job_dir(
        tmp_path,
        inp_text=_FREQ_INP,
        out_text=_out_text(route="B3LYP def2-SVP Freq"),
    )
    earlier = tmp_path / "rxn_attempt1.out"
    earlier.write_text(_out_text(route="B3LYP def2-SVP Freq", freq_block=True), encoding="utf-8")
    state["attempts"].insert(0, dict(state["attempts"][0], index=1, out_path=str(earlier)))

    data = collect_sp_report_data(tmp_path, state)

    assert data is not None
    assert data.imaginary_count == 1
