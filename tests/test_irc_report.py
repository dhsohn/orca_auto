from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import collect_irc_report_data, write_job_html_report
from orca_auto.orca.report.irc import parse_irc_output
from orca_auto.orca.state import write_report_files

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

_IRC_BLOCK = """
----------------------
IRC settings
----------------------
Direction                               .... both
Maximum number of IRC steps             .... 30
Initial Hessian                         .... Calc_Hess
Gradient tolerance                      .... 0.000300
Writing forward trajectory file         .... job_IRC_F.xyz
Writing backward trajectory file        .... job_IRC_B.xyz
Writing full trajectory file            .... job_IRC_Full.xyz

FORWARD IRC
Iter Step E(Eh) max(|G|) RMS(G)
  0   0  -343.997280  0.00200  0.00090
  1   1  -344.020000  0.00150  0.00070
  2   2  -344.050000  0.00100  0.00050

BACKWARD IRC
Iter Step E(Eh) max(|G|) RMS(G)
  0   0  -343.997280  0.00200  0.00090
  1  -1  -344.015000  0.00160  0.00080
  2  -2  -344.045000  0.00110  0.00055

----------------------
IRC PATH SUMMARY
----------------------
Step     E(Eh)        dE(kcal/mol)  max(|G|)  RMS(G)
 -2    -344.045000    -29.95       0.00110   0.00055
 -1    -344.015000    -11.12       0.00160   0.00080
  0    -343.997280      0.00       0.00200   0.00090 <= TS
  1    -344.020000    -14.26       0.00150   0.00070
  2    -344.050000    -33.08       0.00100   0.00050

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


def _state(reaction_dir: Path, out_path: Path) -> dict[str, Any]:
    return {
        "job_id": "job_irc",
        "run_id": "run_irc",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "max_retries": 1,
        "status": "completed",
        "started_at": "2026-07-07T01:00:00+00:00",
        "updated_at": "2026-07-07T01:12:00+00:00",
        "attempts": [
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
        ],
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
    assert parsed.settings[0].label == "Direction"
    assert any(setting.value == "job_IRC_Full.xyz" for setting in parsed.settings)
    assert [point.direction for point in parsed.iterations] == [
        "FORWARD",
        "FORWARD",
        "FORWARD",
        "BACKWARD",
        "BACKWARD",
        "BACKWARD",
    ]
    assert len(parsed.path_points) == 5
    ts = next(point for point in parsed.path_points if point.marker == "TS")
    assert ts.step == 0
    assert ts.energy_hartree == pytest.approx(-343.99728)


def test_collect_irc_report_data_summarizes_path(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    data = collect_irc_report_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.orca_version == "6.0.1"
    assert data.path_points[-1].relative_kcal == pytest.approx(-33.08)
    assert data.attempts[0].detail == "5 path pts, 6 IRC iter"
    assert data.optimization_steps == ()


def test_irc_report_html_renders_path_profile(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    path = write_job_html_report(tmp_path, _state(tmp_path, out_path))

    assert path == tmp_path / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "IRC report" in text
    assert "IRC path profile" in text
    assert "TS marker" in text
    assert "path endpoint 1" in text
    assert "job_IRC_Full.xyz" in text
    assert "kcal mol⁻¹" in text
    assert "<polyline" in text
    assert "TS optimization convergence" not in text


def test_combined_optts_freq_irc_route_renders_composite_sections(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! OptTS Freq IRC B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! OptTS Freq IRC B3LYP def2-SVP", freq=True, opt=True)

    path = write_job_html_report(tmp_path, _state(tmp_path, out_path))

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

    path = write_job_html_report(tmp_path, _state(tmp_path, out_path))

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "No IRC path-summary points were parsed" in text
    assert "IRC setup" in text


def test_write_report_files_emits_irc_html_and_summary_si(tmp_path: Path) -> None:
    _write_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    _write_out(out_path, route="! B3LYP def2-SVP IRC")

    reports = write_report_files(tmp_path, _state(tmp_path, out_path))

    assert reports["report_html"] == str(tmp_path / "job_report.html")
    assert reports["si_block"] == str(tmp_path / "si_block.md")
    si_text = (tmp_path / "si_block.md").read_text(encoding="utf-8")
    assert "IRC validation summary" in si_text
    assert "C      0.000000" not in si_text
