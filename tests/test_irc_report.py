from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import write_job_html_report
from orca_auto.orca.report.irc import collect_irc_report_data, parse_irc_output
from orca_auto.orca.state import write_report_files
from tests.engine_artifact_helpers import bind_report_generation, report_generation_target

_COORDS_BLOCK = """
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  C      0.000000    0.000000    0.000000
  H      0.000000    0.000000    1.089000

"""

_FREQ_BLOCK = """
VIBRATIONAL FREQUENCIES
-----------------------

     0:       0.00 cm**-1
     1:       0.00 cm**-1
     2:       0.00 cm**-1
     3:       0.00 cm**-1
     4:       0.00 cm**-1
     5:       0.00 cm**-1
     6:    -500.00 cm**-1 ***imaginary mode***
     7:     120.00 cm**-1

NORMAL MODES
------------

                  6          7
      0       0.700000   0.100000
      1       0.000000   0.000000
      2       0.000000   0.000000
      3      -0.500000   0.000000
      4       0.000000   0.200000
      5       0.000000   0.100000

IR SPECTRUM
"""

_OPT_BLOCK = """
----------------------------
GEOMETRY OPTIMIZATION CYCLE   1
----------------------------
FINAL SINGLE POINT ENERGY     -343.950000000000

----------------------------
GEOMETRY OPTIMIZATION CYCLE   2
----------------------------
FINAL SINGLE POINT ENERGY     -343.997280000000

THE OPTIMIZATION HAS CONVERGED
"""

# Mirrors the real ORCA 6 IRC driver output: a banner (no "IRC settings"
# header), dotted settings with periods inside labels and leaders butting the
# label, asterisk-boxed direction banners, and iteration rows without a
# separate step column.
_IRC_BLOCK = """
--------------------------------------------------------------------------------
                   Intrinsic Reaction Coordinate Calculation
--------------------------------------------------------------------------------

System:
Nr. of atoms                        .... 2
Algorithm: SD (steepest descent step) plus correction
Settings:
Max. no of cycles        MaxIter    .... 30
Direction                           .... Forward and backward
Initial displacement type           .... Energy
  Initial displacement energy change.... 2.000 mEh
Convergence Tolerances:
  Max. Gradient            TolMAXG  ....  2.0000e-03 Eh/bohr
Storing full IRC trajectory in      .... job_IRC_Full_trj.xyz
Storing forward trajectory in       .... job_IRC_F_trj.xyz
Storing backward trajectory in      .... job_IRC_B_trj.xyz

         *************************************************************
         *                          FORWARD IRC                      *
         *************************************************************

Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
Convergence thresholds                0.002000  0.000500
    0     -343.997280    0.000000    0.002000  0.000900
    1     -344.020000  -14.257000    0.001500  0.000700
    2     -344.050000  -33.081000    0.001000  0.000500

                      ***********************HURRAY********************
                      ***            THE IRC HAS CONVERGED          ***
                      *************************************************

         *************************************************************
         *                          BACKWARD IRC                     *
         *************************************************************

Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
Convergence thresholds                0.002000  0.000500
    0     -344.015000  -11.123000    0.001600  0.000800
    1     -344.045000  -29.947000    0.001100  0.000550

                      ***********************HURRAY********************
                      ***            THE IRC HAS CONVERGED          ***
                      *************************************************

---------------------------------------------------------------
                       IRC PATH SUMMARY
---------------------------------------------------------------
All gradients are in Eh/Bohr.

Step        E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
   1     -344.045000   -29.947000    0.001100  0.000550
   2     -344.015000   -11.123000    0.001600  0.000800
   3     -343.997280     0.000000    0.000200  0.000033 <= TS
   4     -344.020000   -14.257000    0.001500  0.000700
   5     -344.050000   -33.081000    0.001000  0.000500

"""


def _write_inp(path: Path, route: str) -> None:
    path.write_text(f"{route}\n* xyz 0 1\nC 0 0 0\nH 0 0 1\n*\n", encoding="utf-8")


def _write_out(
    path: Path,
    *,
    route: str,
    irc_block: str = _IRC_BLOCK,
    freq: bool = False,
    opt: bool = False,
) -> None:
    path.write_text(
        "\n".join(
            [
                "                                 Program Version 6.0.1 -  RELEASE  -",
                f"|  1> {route}",
                "|  2> * xyz 0 1",
                "|  3> C 0.0 0.0 0.0",
                "|  4> *",
                _COORDS_BLOCK,
                _OPT_BLOCK if opt else "",
                "FINAL SINGLE POINT ENERGY     -343.997280000000",
                _FREQ_BLOCK if freq else "",
                irc_block,
                "                             ****ORCA TERMINATED NORMALLY****",
                "TOTAL RUN TIME: 0 days 0 hours 12 minutes 0 seconds 0 msec",
            ]
        ),
        encoding="utf-8",
    )


def _state(
    reaction_dir: Path,
    out_path: Path,
    *,
    extra_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = [
        {
            "index": 1,
            "inp_path": str(reaction_dir / "rxn.inp"),
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {"irc_marker_found": True},
            "patch_actions": [],
            "started_at": "2026-07-07T01:00:00+00:00",
            "ended_at": "2026-07-07T01:12:00+00:00",
        }
    ]
    attempts.extend(extra_attempts or [])
    return {
        "job_id": "job_irc",
        "run_id": "run_irc",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "status": "completed",
        "started_at": "2026-07-07T01:00:00+00:00",
        "updated_at": "2026-07-07T01:12:00+00:00",
        "attempts": attempts,
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "2026-07-07T01:12:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_parse_irc_output_reads_settings_iterations_and_path_summary(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    parsed = parse_irc_output(out_path)

    assert parsed.irc_marker_found
    assert parsed.settings[0].label == "Nr. of atoms"
    assert any(setting.label == "Max. no of cycles MaxIter" for setting in parsed.settings)
    assert any(setting.value == "2.000 mEh" for setting in parsed.settings)
    assert any(setting.value == "job_IRC_Full_trj.xyz" for setting in parsed.settings)
    assert [point.direction for point in parsed.iterations] == [
        "FORWARD",
        "FORWARD",
        "FORWARD",
        "BACKWARD",
        "BACKWARD",
    ]
    assert parsed.iterations[2].delta_e_kcal == pytest.approx(-33.081)
    assert len(parsed.path_points) == 5
    ts = next(point for point in parsed.path_points if point.marker == "TS")
    assert ts.step == 3
    assert ts.energy_hartree == pytest.approx(-343.99728)


def test_collect_irc_report_data_summarizes_path(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    data = collect_irc_report_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.orca_version == "6.0.1"
    assert data.path_points[-1].relative_kcal == pytest.approx(-33.081)
    assert data.attempts[0].detail == "5 path pts, 5 IRC iter"
    assert data.optimization_steps == ()


def test_collect_irc_report_data_skips_contentless_final_attempt(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")
    dead_out = tmp_path / "rxn_retry.out"
    dead_out.write_text("ORCA crashed before the IRC driver started\n", encoding="utf-8")

    state = _state(
        tmp_path,
        out_path,
        extra_attempts=[
            {
                "index": 2,
                "inp_path": str(tmp_path / "rxn.inp"),
                "out_path": str(dead_out),
                "return_code": 1,
                "analyzer_status": "failed",
                "analyzer_reason": "abnormal_termination",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-07T01:13:00+00:00",
                "ended_at": "2026-07-07T01:13:30+00:00",
            }
        ],
    )
    data = collect_irc_report_data(tmp_path, state)

    assert data is not None
    assert len(data.path_points) == 5
    assert data.irc_marker_found


def test_irc_report_html_renders_path_profile(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "IRC report" in text
    assert "IRC path profile" in text
    assert "TS marker" in text
    assert "path endpoint 1" in text
    assert "job_IRC_Full_trj.xyz" in text
    assert "kcal mol⁻¹" in text
    assert "<polyline" in text
    assert "TS optimization convergence" not in text


def test_combined_optts_freq_irc_route_renders_composite_sections(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! OptTS Freq IRC B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! OptTS Freq IRC B3LYP def2-SVP", freq=True, opt=True)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "IRC report" in text
    assert "TS report" not in text
    assert "Calculation summary" in text
    assert "TS optimization convergence" in text
    assert "Vibrational summary" in text
    assert "IRC path profile" in text
    assert "TS opt cycles" in text
    assert text.count('<div class="metric-label">Final energy</div>') == 1
    assert text.count('<div class="metric-label">Imaginary frequencies</div>') == 1


def test_irc_report_with_missing_path_summary_has_fallback(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(
        out_path,
        route="! B3LYP def2-SVP IRC",
        irc_block=_IRC_BLOCK.split("IRC PATH SUMMARY", maxsplit=1)[0],
    )

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "No IRC path-summary points were parsed" in text
    assert "IRC setup" in text


def test_multiline_route_classifies_ts_correctly(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP\n! OptTS Freq IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! OptTS Freq IRC B3LYP def2-SVP", freq=True, opt=True)

    data = collect_irc_report_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert "OptTS" in data.route_line
    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "TS optimization convergence" in text
    assert "TS opt cycles" in text
    assert "expected 1" in text


def test_neb_trajectory_not_captured_as_irc_setting(tmp_path: Path) -> None:
    neb_plus_irc_block = ("Writing initial trajectory to file  .... neb_init.xyz\n\n") + _IRC_BLOCK
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! NEB-TS IRC B3LYP def2-SVP", irc_block=neb_plus_irc_block)

    parsed = parse_irc_output(out_path)

    assert not any("neb_init" in s.value for s in parsed.settings)
    assert any("job_IRC_Full_trj.xyz" in s.value for s in parsed.settings)


def test_write_report_files_emits_irc_html_and_summary_si(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    state = _state(tmp_path, out_path)
    generation = bind_report_generation(tmp_path, state)
    reports = write_report_files(tmp_path, state)

    assert reports["report_html"] == str(generation / "job_report.html")
    assert reports["si_block"] == str(generation / "si_block.md")
    si_text = (generation / "si_block.md").read_text(encoding="utf-8")
    assert "IRC validation summary" in si_text
    assert "Storing full IRC trajectory in: job_IRC_Full_trj.xyz" in si_text
    assert "C      0.000000" not in si_text
