from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import collect_opt_report_data, write_job_html_report
from orca_auto.orca.statuses import AnalyzerStatus
from tests.engine_artifact_helpers import report_generation_target

_OPT_CYCLES_BLOCK = """
                *** Geometry Optimization Cycle   1 ***

FINAL SINGLE POINT ENERGY      -100.00000000

                *** Geometry Optimization Cycle   2 ***

FINAL SINGLE POINT ENERGY      -100.00500000

                *** Geometry Optimization Cycle   3 ***

FINAL SINGLE POINT ENERGY      -100.00520000

                    ***********************HURRAY********************
                    ***        THE OPTIMIZATION HAS CONVERGED     ***
                    *************************************************
"""

_COORDS_BLOCK = """
---------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  H      0.000000    0.000000    0.000000
  O      1.200000    0.000000    0.000000
  O      3.000000    0.000000    0.000000

"""

_FREQ_TS_BLOCK = """
-----------------------
VIBRATIONAL FREQUENCIES
-----------------------

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    -410.20 cm**-1 ***imaginary mode***
     7:     120.00 cm**-1
     8:     300.00 cm**-1

------------
NORMAL MODES
------------

                  0          1          2          3          4          5
      0       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      1       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      2       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      3       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      4       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      5       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      6       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      7       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
      8       0.000000   0.000000   0.000000   0.000000   0.000000   0.000000
                  6          7          8
      0       0.800000   0.100000   0.000000
      1       0.000000   0.000000   0.100000
      2       0.000000   0.000000   0.000000
      3      -0.400000   0.200000   0.000000
      4       0.000000   0.000000   0.300000
      5       0.000000   0.000000   0.000000
      6       0.000000   0.500000   0.000000
      7       0.000000   0.000000   0.700000
      8       0.000000   0.000000   0.000000

IR SPECTRUM
"""


def _write_inp(path: Path, route: str) -> None:
    path.write_text(f"{route}\n\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")


def _write_opt_out(path: Path, *, freq_block: str = "") -> None:
    path.write_text(
        "! Opt B3LYP def2-SVP\n"
        + _COORDS_BLOCK
        + _OPT_CYCLES_BLOCK
        + freq_block
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def _state(reaction_dir: Path, out_path: Path, *, reason: str) -> dict[str, Any]:
    return {
        "job_id": "job_test",
        "run_id": "run_test",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "max_retries": 3,
        "status": "completed",
        "started_at": "2026-07-03T01:00:00+00:00",
        "updated_at": "2026-07-03T04:00:00+00:00",
        "attempts": [
            {
                "index": 1,
                "inp_path": str(reaction_dir / "rxn.inp"),
                "out_path": str(out_path),
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": reason,
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-03T01:00:00+00:00",
                "ended_at": "2026-07-03T02:15:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": reason,
            "completed_at": "2026-07-03T02:15:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_collect_opt_report_parses_cycles_and_convergence(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_opt_out(out_path)

    data = collect_opt_report_data(
        tmp_path, _state(tmp_path, out_path, reason="normal_termination"), kind="opt"
    )

    assert data is not None
    assert data.kind == "opt"
    assert [cycle for cycle, _ in data.steps] == [1, 2, 3]
    assert data.final_energy == pytest.approx(-100.0052)
    assert data.opt_converged
    assert data.imaginary_count is None
    assert data.mode_summaries == ()


def test_collect_opt_report_skips_contentless_final_attempt(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_opt_out(out_path)
    dead_out = tmp_path / "rxn_retry.out"
    dead_out.write_text("ORCA crashed before the first cycle\n", encoding="utf-8")

    state = _state(tmp_path, out_path, reason="normal_termination")
    state["attempts"].append(
        {
            "index": 2,
            "inp_path": str(tmp_path / "rxn.inp"),
            "out_path": str(dead_out),
            "return_code": 1,
            "analyzer_status": "failed",
            "analyzer_reason": "abnormal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-07-03T02:16:00+00:00",
            "ended_at": "2026-07-03T02:16:30+00:00",
        }
    )
    data = collect_opt_report_data(tmp_path, state, kind="opt")

    assert data is not None
    assert [cycle for cycle, _ in data.steps] == [1, 2, 3]
    assert data.final_energy == pytest.approx(-100.0052)
    assert data.opt_converged


def test_opt_report_html_renders_convergence_chart(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_opt_out(out_path)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="normal_termination"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "Opt report" in text
    assert "Optimization convergence" in text
    assert "<polyline" in text
    assert "initial Opt" in text
    assert "converged" in text
    assert "No frequency calculation found" in text


def test_attempt_table_normalizes_live_analyzer_status_enum(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_opt_out(out_path)
    state = _state(tmp_path, out_path, reason="normal_termination")
    state["attempts"][0]["analyzer_status"] = AnalyzerStatus.COMPLETED

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert '<td class="ok">completed<div class="sub">normal_termination</div></td>' in text
    assert "AnalyzerStatus.COMPLETED" not in text


def test_frequency_without_mode_vectors_is_not_reported_as_missing_calculation(
    tmp_path: Path,
) -> None:
    _write_inp(tmp_path / "rxn.inp", "! OptTS B3LYP def2-SVP Freq")
    out_path = tmp_path / "rxn.out"
    frequency_only = _FREQ_TS_BLOCK.split("------------\nNORMAL MODES", maxsplit=1)[0]
    _write_opt_out(out_path, freq_block=frequency_only)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="ts_criteria_met"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Frequency values were parsed" in text
    assert "no usable normal-mode displacement vectors were available" in text
    assert "No frequency calculation found" not in text


def test_optts_report_summarizes_imaginary_mode(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! OptTS B3LYP def2-SVP Freq")
    out_path = tmp_path / "rxn.out"
    _write_opt_out(out_path, freq_block=_FREQ_TS_BLOCK)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="ts_criteria_met"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "TS report" in text
    assert "initial OptTS" in text
    assert "imaginary mode" in text
    assert "-410.2" in text
    assert "as expected for a TS" in text
    assert "TS criteria met" in text


def test_opt_report_flags_unexpected_imaginary_mode(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! Opt Freq B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_opt_out(out_path, freq_block=_FREQ_TS_BLOCK)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="normal_termination"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Opt report" in text
    assert "expected 0 for a minimum" in text
