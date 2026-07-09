from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import (
    collect_scants_report_data,
    parse_frequency_analysis,
    write_job_html_report,
)
from orca_auto.orca.state import write_report_files

_FREQ_BLOCK = """
-----------------------
VIBRATIONAL FREQUENCIES
-----------------------

Scaling factor for frequencies =  1.000000000  (already applied!)

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    -155.30 cm**-1 ***imaginary mode***
     7:     120.00 cm**-1
     8:     300.00 cm**-1
"""

_MODES_BLOCK = """
------------
NORMAL MODES
------------

These modes are the Cartesian displacements weighted by the diagonal matrix
M(i,i)=1/sqrt(m[i]) where m[i] is the mass of the displaced atom
Thus, these vectors are normalized but *not* orthogonal

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
      0       0.900000   0.100000   0.000000
      1       0.000000   0.000000   0.100000
      2       0.000000   0.000000   0.000000
      3      -0.300000   0.200000   0.000000
      4       0.000000   0.000000   0.300000
      5       0.000000   0.000000   0.000000
      6       0.000000   0.500000   0.000000
      7       0.000000   0.000000   0.700000
      8       0.000000   0.000000   0.000000

IR SPECTRUM
"""

_COORDS_BLOCK = """
---------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  H      0.000000    0.000000    0.000000
  O      1.200000    0.000000    0.000000
  O      3.000000    0.000000    0.000000

"""

_SURFACE_BLOCK = """
RELAXED SURFACE SCAN RESULTS

The Calculated Surface using the 'Actual Energy'
   1.86000000 -100.00000000
   1.91000000 -99.99000000
   1.96000000 -100.02000000

The Calculated Surface using the SCF energy
   1.86000000 -101.00000000
"""

_OPT_CYCLES_BLOCK = """
                *** Geometry Optimization Cycle   1 ***

FINAL SINGLE POINT ENERGY      -100.01000000

                *** Geometry Optimization Cycle   2 ***

FINAL SINGLE POINT ENERGY      -100.02000000

                    ***********************HURRAY********************
                    ***        THE OPTIMIZATION HAS CONVERGED     ***
                    *************************************************
"""

_IRC_BLOCK = """
--------------------------------------------------------------------------------
                   Intrinsic Reaction Coordinate Calculation
--------------------------------------------------------------------------------

Settings:
Direction                           .... both
Storing full IRC trajectory in      .... scants_IRC_Full.xyz

----------------------
IRC PATH SUMMARY
----------------------
All gradients are in Eh/Bohr.

Step     E(Eh)        dE(kcal/mol)  max(|G|)  RMS(G)
  1    -100.050000    -18.83       0.00160   0.00080
  2    -100.020000      0.00       0.00200   0.00090 <= TS
  3    -100.060000    -25.10       0.00150   0.00070

"""


def _write_ts_out(path: Path) -> None:
    path.write_text(
        _COORDS_BLOCK
        + _SURFACE_BLOCK
        + _FREQ_BLOCK
        + _MODES_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def _write_scants_irc_out(path: Path) -> None:
    path.write_text(
        _COORDS_BLOCK
        + _SURFACE_BLOCK
        + _OPT_CYCLES_BLOCK
        + _FREQ_BLOCK
        + _MODES_BLOCK
        + _IRC_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def _write_scants_inp(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "! ScanTS B3LYP def2-SVP Freq",
                "",
                "%geom",
                "  Scan",
                "    B 0 1 = 1.86, 1.96, 3",
                "  end",
                "end",
                "",
                "* xyzfile 0 1 input.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _state(reaction_dir: Path, out_path: Path) -> dict[str, Any]:
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
                "analyzer_reason": "ts_criteria_met",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-03T01:00:00+00:00",
                "ended_at": "2026-07-03T03:48:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "ts_criteria_met",
            "completed_at": "2026-07-03T03:48:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_parse_frequency_analysis_reads_last_blocks(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    stale = _FREQ_BLOCK.replace("-155.30", "-999.00")
    out_path.write_text(
        _COORDS_BLOCK + stale + _COORDS_BLOCK + _FREQ_BLOCK + _MODES_BLOCK,
        encoding="utf-8",
    )

    analysis = parse_frequency_analysis(out_path)

    assert analysis is not None
    assert len(analysis.frequencies) == 9
    assert analysis.frequencies[6] == pytest.approx(-155.30)
    assert [atom[0] for atom in analysis.atoms] == ["H", "O", "O"]
    vector = analysis.mode_vector(6)
    assert vector[0] == pytest.approx(0.9)
    assert vector[3] == pytest.approx(-0.3)
    assert vector[8] == pytest.approx(0.0)


def test_parse_frequency_analysis_without_freq_block(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    out_path.write_text(_COORDS_BLOCK + _SURFACE_BLOCK, encoding="utf-8")
    assert parse_frequency_analysis(out_path) is None


def test_collect_summarizes_imaginary_mode_and_alignment(tmp_path: Path) -> None:
    _write_scants_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    _write_ts_out(out_path)

    data = collect_scants_report_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.imaginary_count == 1
    assert len(data.mode_summaries) == 1
    summary = data.mode_summaries[0]
    assert summary.imaginary
    assert summary.frequency_cm == pytest.approx(-155.30)
    top = summary.top_atoms[0]
    assert (top.element, top.atom_index) == ("H", 0)
    # Mode 6: H moves +x, first O moves -x; scanned bond B(0,1) lies on x, so
    # the alignment is |(0.9 - (-0.3))| / sqrt(2).
    assert summary.scan_alignment == pytest.approx(1.2 / 2**0.5, rel=1e-6)
    assert data.forward_barrier_kcal is not None
    assert data.forward_barrier_kcal > 0.5
    assert data.segments[0].points[0].coordinates[0] == pytest.approx(1.86)


def test_collect_returns_none_for_non_scants_input(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text("! Opt B3LYP def2-SVP\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    out_path = tmp_path / "rxn.out"
    _write_ts_out(out_path)

    assert collect_scants_report_data(tmp_path, _state(tmp_path, out_path)) is None


def test_write_job_html_report_renders_scants_sections(tmp_path: Path) -> None:
    _write_scants_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    _write_ts_out(out_path)

    path = write_job_html_report(tmp_path, _state(tmp_path, out_path))

    assert path == tmp_path / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "ScanTS report" in text
    assert "ts_criteria_met" in text
    assert "<polyline" in text
    assert "initial ScanTS" in text
    assert "imaginary mode" in text
    assert "B(0,1)" in text
    assert "85%" in text
    assert "TS criteria met" in text


def test_relaxed_scan_gets_profile_report_not_opt_report(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text(
        "\n".join(
            [
                "! Opt B3LYP def2-SVP",
                "",
                "%geom",
                "  Scan",
                "    B 0 1 = 1.86, 1.96, 3",
                "  end",
                "end",
                "",
                "* xyzfile 0 1 input.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    _write_ts_out(out_path)

    path = write_job_html_report(tmp_path, _state(tmp_path, out_path))

    assert path == tmp_path / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "Relaxed scan report" in text
    assert "ScanTS" not in text
    assert "Scan energy profile" in text
    assert "<polyline" in text
    assert "initial relaxed scan" in text
    assert "Interior barrier" in text
    assert "prominence over the shallower flank" in text
    assert "Optimization convergence" not in text
    # Freq block present in the fixture out: the vibrational summary and the
    # scan-coordinate alignment apply to relaxed scans too.
    assert "B(0,1)" in text
    assert "85%" in text


def test_scants_irc_report_does_not_treat_scan_cycles_as_ts_refinement(
    tmp_path: Path,
) -> None:
    _write_scants_inp(tmp_path / "rxn.inp")
    (tmp_path / "rxn.inp").write_text(
        (tmp_path / "rxn.inp").read_text(encoding="utf-8").replace("Freq", "Freq IRC"),
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    _write_scants_irc_out(out_path)

    path = write_job_html_report(tmp_path, _state(tmp_path, out_path))

    assert path == tmp_path / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "ScanTS report" in text
    assert "Scan energy profile" in text
    assert "IRC path profile" in text
    assert "scants_IRC_Full.xyz" in text
    assert "TS optimization convergence" not in text
    assert text.count('<div class="metric-label">Imaginary frequencies</div>') == 1
    assert "initial ScanTS" in text


def test_write_report_files_includes_html_for_scants(tmp_path: Path) -> None:
    _write_scants_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    _write_ts_out(out_path)

    reports = write_report_files(tmp_path, _state(tmp_path, out_path))

    assert reports["report_html"] == str(tmp_path / "job_report.html")
    assert (tmp_path / "job_report.html").exists()
    assert (tmp_path / "job_report.md").exists()


def test_write_report_files_skips_html_and_removes_stale_for_md(
    tmp_path: Path,
) -> None:
    # MD is a non-stationary job type with no HTML report (single points and IRC
    # now have their own report flavors, so they no longer cover this path).
    inp = tmp_path / "rxn.inp"
    inp.write_text("! B3LYP def2-SVP MD\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    out_path = tmp_path / "rxn.out"
    _write_ts_out(out_path)
    # Leftover report from a previous Opt/ScanTS job in this reused reaction dir
    # must not survive, or downstream links would surface an obsolete report.
    stale = tmp_path / "job_report.html"
    stale.write_text("<html>old opt report</html>", encoding="utf-8")

    reports = write_report_files(tmp_path, _state(tmp_path, out_path))

    assert "report_html" not in reports
    assert not stale.exists()
