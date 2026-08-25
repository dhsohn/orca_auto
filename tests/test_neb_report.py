from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import write_job_html_report
from orca_auto.orca.report.neb import NebPathPoint, _path_plot_x, collect_neb_report_data
from orca_auto.orca.report.render import ChartSeries, line_chart_svg
from tests.engine_artifact_helpers import report_generation_target

_COORDS_BLOCK = """
---------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  H      0.000000    0.000000    0.000000
  O      1.200000    0.000000    0.000000
  O      3.000000    0.000000    0.000000

"""

_OPT_CYCLES_BLOCK = """
                *** Geometry Optimization Cycle   1 ***

FINAL SINGLE POINT ENERGY      -343.99900000

                *** Geometry Optimization Cycle   2 ***

FINAL SINGLE POINT ENERGY      -343.99864000

"""

_NEB_BLOCK = """
----------------------
NEB settings
----------------------
Method type                             ....  climbing image
Tangent type                            ....  improved
Number of intermediate images           ....  8
Generation of initial path              ....  image dependent pair potential
Initial path via TS guess               ....  off

Optimization method:
Method                                  ....  L-BFGS
Max. iterations                         ....  500

Generation of  the initial path:
Writing initial trajectory to file      ....  nebts_initial_path_trj.xyz

Starting iterations:
Optim.  Iteration  HEI  E(HEI)-E(0)  max(|Fp|)   RMS(Fp)    dS
Switch-on CI threshold               0.020000
   LBFGS     0      5    0.372340    0.129615   0.029150  12.1839
   LBFGS     1      5    0.352980    0.104320   0.024586  12.1349
Image  6 will be converted to a climbing image in the next iteration (max(|Fp|) < 0.0200)
Optim.  Iteration  CI   E(CI)-E(0)   max(|Fp|)   RMS(Fp)    dS     max(|FCI|)   RMS(FCI)
Convergence thresholds               0.020000   0.010000            0.002000    0.001000
   LBFGS    49      6    0.130964    0.016940   0.003526  15.3056    0.017426    0.005167
   LBFGS    50      6    0.129316    0.045416   0.007419  15.4226    0.040132    0.010358

                    *********************H U R R A Y*********************
                    ***        THE NEB OPTIMIZATION HAS CONVERGED     ***
                    *****************************************************

                    ***********************HURRAY************************
                    ***        THE TS OPTIMIZATION HAS CONVERGED      ***
                    *****************************************************
---------------------------------------------------------------
                      PATH SUMMARY FOR NEB-TS
---------------------------------------------------------------
All forces in Eh/Bohr. Global forces for TS.
Image     E(Eh)   dE(kcal/mol)  max(|Fp|)  RMS(Fp)
  0    -344.08225     0.00       0.00014   0.00003
  1    -344.07675     3.45       0.00232   0.00071
  2    -344.07387     5.26       0.01427   0.00363
  3    -344.07067     7.27       0.00282   0.00105
  4    -344.04841    21.23       0.00130   0.00058
  5    -344.01506    42.16       0.00124   0.00056
  6    -343.99728    53.32       0.00150   0.00060 <= CI
 TS    -343.99864    52.47       0.00013   0.00005 <= TS
  7    -344.03091    32.22       0.00151   0.00061
  8    -344.06788     9.02       0.00179   0.00073
  9    -344.07676     3.45       0.00026   0.00009

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

_IRC_BLOCK = """
--------------------------------------------------------------------------------
                   Intrinsic Reaction Coordinate Calculation
--------------------------------------------------------------------------------

Settings:
Direction                           .... both
Storing full IRC trajectory in      .... neb_IRC_Full.xyz

----------------------
IRC PATH SUMMARY
----------------------
All gradients are in Eh/Bohr.

Step     E(Eh)        dE(kcal/mol)  max(|G|)  RMS(G)
  1    -344.015000    -11.12       0.00160   0.00080
  2    -343.998640      0.00       0.00200   0.00090 <= TS
  3    -344.020000    -14.26       0.00150   0.00070

"""


def _write_neb_inp(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "! NEB-TS B3LYP def2-SVP Freq",
                "",
                "%neb",
                '  neb_end_xyzfile "product.xyz"',
                "end",
                "",
                "* xyzfile 0 1 reactant.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_neb_out(path: Path) -> None:
    path.write_text(
        "! NEB-TS B3LYP def2-SVP Freq\n"
        + _COORDS_BLOCK
        + _NEB_BLOCK
        + _OPT_CYCLES_BLOCK
        + _FREQ_TS_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def _write_neb_irc_out(path: Path) -> None:
    path.write_text(
        "! NEB-TS B3LYP def2-SVP Freq IRC\n"
        + _COORDS_BLOCK
        + _NEB_BLOCK
        + _OPT_CYCLES_BLOCK
        + _FREQ_TS_BLOCK
        + _IRC_BLOCK
        + "\n****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )


def _state(reaction_dir: Path, out_path: Path) -> dict[str, Any]:
    return {
        "job_id": "job_neb",
        "run_id": "run_neb",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "max_retries": 1,
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
                "ended_at": "2026-07-03T04:00:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "ts_criteria_met",
            "completed_at": "2026-07-03T04:00:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_collect_neb_report_data_parses_path_and_iterations(tmp_path: Path) -> None:
    _write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    _write_neb_out(out_path)

    data = collect_neb_report_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.neb_converged
    assert data.ts_converged
    assert len(data.path_points) == 11
    ci = next(point for point in data.path_points if point.marker == "CI")
    ts = next(point for point in data.path_points if point.marker == "TS")
    assert ci.label == "6"
    assert ci.relative_kcal == pytest.approx(53.32)
    assert ts.label == "TS"
    assert ts.energy_hartree == pytest.approx(-343.99864)
    assert [point.iteration for point in data.iterations] == [0, 1, 49, 50]
    assert data.iterations[-1].phase == "CI"
    assert data.ts_steps == ((1, -343.999), (2, -343.99864))
    assert data.imaginary_count == 1
    assert data.settings[0].label == "Method type"
    # "Generation of initial path ...." is a setting row, not the section
    # terminator, and dotted labels may contain periods ("Max. iterations");
    # the table must run up to the "Generation of  the initial path:" line.
    labels = [setting.label for setting in data.settings]
    assert "Generation of initial path" in labels
    assert "Max. iterations" in labels
    assert not any("nebts_initial_path_trj" in setting.value for setting in data.settings)


def test_collect_neb_report_data_skips_contentless_final_attempt(tmp_path: Path) -> None:
    _write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    _write_neb_out(out_path)
    dead_out = tmp_path / "rxn_retry.out"
    dead_out.write_text("ORCA crashed before the NEB driver started\n", encoding="utf-8")

    state = _state(tmp_path, out_path)
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
            "started_at": "2026-07-03T04:01:00+00:00",
            "ended_at": "2026-07-03T04:01:30+00:00",
        }
    )
    data = collect_neb_report_data(tmp_path, state)

    assert data is not None
    assert len(data.path_points) == 11
    assert data.neb_converged
    assert data.ts_steps == ((1, -343.999), (2, -343.99864))


def test_neb_path_plot_x_places_ts_between_neighboring_images() -> None:
    points = (
        NebPathPoint("4", 4, 4, -1.0, 10.0, 0.1, 0.1, ""),
        NebPathPoint("TS", 5, None, -0.9, 20.0, 0.1, 0.1, "TS"),
        NebPathPoint("5", 6, 5, -0.8, 15.0, 0.1, 0.1, ""),
    )

    assert _path_plot_x(points, 0) == pytest.approx(4.0)
    assert _path_plot_x(points, 1) == pytest.approx(4.5)
    assert _path_plot_x(points, 2) == pytest.approx(5.0)


def test_line_chart_svg_uses_marker_legend_for_single_point_series() -> None:
    svg = line_chart_svg(
        (
            ChartSeries(label="selected point", color="#158a72", dash="", points=((0.5, 0.5),)),
            ChartSeries(label="path", color="#2f6fb2", dash="", points=((0.0, 0.0), (1.0, 1.0))),
        ),
        x_label="image",
        y_label="energy",
    )

    assert '<circle cx="90" cy="24" r="3.5" fill="#158a72"/>' in svg
    assert '<line x1="76" y1="24" x2="104" y2="24" stroke="#158a72"' not in svg


def test_neb_ts_report_renders_neb_specific_sections(tmp_path: Path) -> None:
    _write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    _write_neb_out(out_path)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "NEB-TS report" in text
    assert "NEB-CI path profile" in text
    assert "NEB-CI optimization" in text
    assert "TS optimization convergence" in text
    assert "NEB setup" in text
    assert "Number of intermediate images" in text
    assert "climbing image" in text
    assert "optimized TS" in text
    assert "CI-NEB converged" in text
    assert "TS optimized" in text
    assert "initial NEB-TS" in text
    assert "imaginary mode" in text
    assert "kcal mol⁻¹" in text
    assert "mol^-1" not in text
    assert "<polyline" in text


def test_neb_ts_irc_report_composes_neb_and_irc_sections(tmp_path: Path) -> None:
    _write_neb_inp(tmp_path / "rxn.inp")
    (tmp_path / "rxn.inp").write_text(
        (tmp_path / "rxn.inp").read_text(encoding="utf-8").replace("Freq", "Freq IRC"),
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    _write_neb_irc_out(out_path)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "NEB-TS report" in text
    assert "NEB-CI path profile" in text
    assert "IRC path profile" in text
    assert "neb_IRC_Full.xyz" in text
    assert "IRC path found" in text
    assert "Vibrational summary" in text
    assert "initial NEB-TS" in text
    assert text.count('<div class="metric-label">Imaginary frequencies</div>') == 1
