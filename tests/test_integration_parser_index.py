"""Integration tests: realistic ORCA outputs → parser → DFT index pipeline.

Each fixture mirrors the structure of a real ORCA .out file with representative
sections (input line, coordinates, energy, convergence, frequencies, thermo,
termination, runtime).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.orca.dft.index import DFTIndex
from orca_auto.orca.dft.monitor import DFTMonitor
from orca_auto.orca.parser import parse_orca_output
from tests.engine_artifact_helpers import orca_artifact_payload

# ---------------------------------------------------------------------------
# Realistic ORCA output fixtures
# ---------------------------------------------------------------------------

_B3LYP_OPT_FREQ_COMPLETED = """\
|  1> ! B3LYP 6-31G(d) Opt Freq
|  2> * xyz 0 1
|  3>   C    0.000000    0.000000    0.000000
|  4>   O    1.200000    0.000000    0.000000
|  5>   H   -0.500000    0.866025    0.000000
|  6>   H   -0.500000   -0.866025    0.000000
|  7> *

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 C    0.000000    0.000000    0.000000
 O    1.200000    0.000000    0.000000
 H   -0.500000    0.866025    0.000000
 H   -0.500000   -0.866025    0.000000

---------------------------------------------------
| Geometry Optimization Cycle   1                 |
---------------------------------------------------

FINAL SINGLE POINT ENERGY      -113.800000000

                         *************************************
                         *  GEOMETRY CONVERGENCE              *
                         *************************************
Item                Value     Tolerance   Converged
Energy change       0.000000  5.0000e-06    YES
MAX gradient        0.010000  3.0000e-04    NO
RMS gradient        0.005000  1.0000e-04    NO
MAX step            0.020000  4.0000e-03    NO
RMS step            0.008000  2.0000e-03    NO

---------------------------------------------------
| Geometry Optimization Cycle   2                 |
---------------------------------------------------

FINAL SINGLE POINT ENERGY      -113.850000000

                         *************************************
                         *  GEOMETRY CONVERGENCE              *
                         *************************************
Item                Value     Tolerance   Converged
Energy change      -0.050000  5.0000e-06    NO
MAX gradient        0.000200  3.0000e-04    YES
RMS gradient        0.000050  1.0000e-04    YES
MAX step            0.001000  4.0000e-03    YES
RMS step            0.000500  2.0000e-03    YES

---------------------------------------------------
| Geometry Optimization Cycle   3                 |
---------------------------------------------------

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 C    0.000100    0.000200    0.000000
 O    1.205000    0.000100    0.000000
 H   -0.520000    0.870000    0.000000
 H   -0.520000   -0.870000    0.000000

FINAL SINGLE POINT ENERGY      -113.867432100

                         *************************************
                         *  GEOMETRY CONVERGENCE              *
                         *************************************
Item                Value     Tolerance   Converged
Energy change      -0.000001  5.0000e-06    YES
MAX gradient        0.000010  3.0000e-04    YES
RMS gradient        0.000005  1.0000e-04    YES
MAX step            0.000100  4.0000e-03    YES
RMS step            0.000050  2.0000e-03    YES

THE OPTIMIZATION HAS CONVERGED

VIBRATIONAL FREQUENCIES
-----------------------
  0:      0.00 cm**-1
  1:      0.00 cm**-1
  2:      0.00 cm**-1
  3:      0.00 cm**-1
  4:      0.00 cm**-1
  5:      0.00 cm**-1
  6:   1167.32 cm**-1
  7:   1251.89 cm**-1
  8:   1534.00 cm**-1
  9:   1780.45 cm**-1
 10:   2843.21 cm**-1
 11:   2917.56 cm**-1

---------------------------

Total Enthalpy                    ... -113.834210 Eh
Final Gibbs free energy           ... -113.862100 Eh

                             ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 2 hours 15 minutes 30 seconds 0 msec
"""

_DLPNO_SP_COMPLETED = """\
! DLPNO-CCSD(T) cc-pVTZ
* xyz 0 1
  N    0.000000    0.000000    0.000000
  H    0.000000    0.000000    1.012000
  H    0.942800    0.000000   -0.336000
  H   -0.471400    0.816500   -0.336000
*

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 N    0.000000    0.000000    0.000000
 H    0.000000    0.000000    1.012000
 H    0.942800    0.000000   -0.336000
 H   -0.471400    0.816500   -0.336000

FINAL SINGLE POINT ENERGY      -56.520893412

                             ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 5 hours 42 minutes 18 seconds 0 msec
"""

_TS_OPT_WITH_IMAGINARY = """\
! B3LYP def2-TZVP OptTS Freq
* xyz 0 1
  C    0.000000    0.000000    0.000000
  H    1.089000    0.000000    0.000000
  H   -0.544500    0.943000    0.000000
  H   -0.544500   -0.471500    0.816500
  H   -0.544500   -0.471500   -0.816500
  Cl   3.000000    0.000000    0.000000
*

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 C    0.000000    0.000000    0.000000
 H    1.089000    0.000000    0.000000
 H   -0.544500    0.943000    0.000000
 H   -0.544500   -0.471500    0.816500
 H   -0.544500   -0.471500   -0.816500
 Cl   3.000000    0.000000    0.000000

FINAL SINGLE POINT ENERGY      -500.123456789

THE OPTIMIZATION HAS CONVERGED

VIBRATIONAL FREQUENCIES
-----------------------
  0:      0.00 cm**-1
  1:      0.00 cm**-1
  2:      0.00 cm**-1
  3:      0.00 cm**-1
  4:      0.00 cm**-1
  5:      0.00 cm**-1
  6:   -432.15 cm**-1
  7:    523.40 cm**-1
  8:    780.10 cm**-1
  9:   1050.32 cm**-1
 10:   1320.45 cm**-1
 11:   1450.78 cm**-1
 12:   1580.90 cm**-1
 13:   2980.12 cm**-1
 14:   3050.45 cm**-1
 15:   3120.78 cm**-1

---------------------------

Total Enthalpy                    ... -500.089123 Eh
Final Gibbs free energy           ... -500.112345 Eh

                             ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 8 hours 30 minutes 45 seconds 0 msec
"""

_SCF_FAILED = """\
! wB97X-D3 def2-TZVP Opt
* xyz -1 2
  Fe   0.000000    0.000000    0.000000
  O    2.000000    0.000000    0.000000
  O   -2.000000    0.000000    0.000000
  O    0.000000    2.000000    0.000000
  O    0.000000   -2.000000    0.000000
*

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 Fe   0.000000    0.000000    0.000000
 O    2.000000    0.000000    0.000000
 O   -2.000000    0.000000    0.000000
 O    0.000000    2.000000    0.000000
 O    0.000000   -2.000000    0.000000

SCF NOT CONVERGED AFTER 300 CYCLES

ORCA finished by error termination in SCF gradient
[file orca_tools/qcmsg.cpp, line 394]:
  .... aborting the run
"""

_OPT_NOT_CONVERGED = """\
! PBE0 def2-SVP Opt
* xyz 0 1
  C    0.000000    0.000000    0.000000
  C    1.540000    0.000000    0.000000
  H   -0.360000    1.020000    0.000000
  H   -0.360000   -0.510000    0.883000
  H   -0.360000   -0.510000   -0.883000
  H    1.900000    1.020000    0.000000
  H    1.900000   -0.510000    0.883000
  H    1.900000   -0.510000   -0.883000
*

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 C    0.000000    0.000000    0.000000
 C    1.540000    0.000000    0.000000
 H   -0.360000    1.020000    0.000000
 H   -0.360000   -0.510000    0.883000
 H   -0.360000   -0.510000   -0.883000
 H    1.900000    1.020000    0.000000
 H    1.900000   -0.510000    0.883000
 H    1.900000   -0.510000   -0.883000

---------------------------------------------------
| Geometry Optimization Cycle   1                 |
---------------------------------------------------

FINAL SINGLE POINT ENERGY      -79.500000000

---------------------------------------------------
| Geometry Optimization Cycle  50                 |
---------------------------------------------------

FINAL SINGLE POINT ENERGY      -79.650000000

ORCA GEOMETRY OPTIMIZATION - DID NOT CONVERGE

                             ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 12 hours 0 minutes 0 seconds 0 msec
"""

_RUNNING_M06_2X = """\
! M06-2X 6-311+G(d,p) Opt
* xyz 1 1
  C    0.000000    0.000000    0.000000
  N    1.470000    0.000000    0.000000
  H   -0.360000    1.020000    0.000000
  H   -0.360000   -0.510000    0.883000
  H   -0.360000   -0.510000   -0.883000
  H    1.830000    0.940000    0.000000
  H    1.830000   -0.470000    0.816000
  H    1.830000   -0.470000   -0.816000
*

CARTESIAN COORDINATES (ANGSTROEM)
----------------------------
 C    0.000000    0.000000    0.000000
 N    1.470000    0.000000    0.000000
 H   -0.360000    1.020000    0.000000
 H   -0.360000   -0.510000    0.883000
 H   -0.360000   -0.510000   -0.883000
 H    1.830000    0.940000    0.000000
 H    1.830000   -0.470000    0.816000
 H    1.830000   -0.470000   -0.816000

---------------------------------------------------
| Geometry Optimization Cycle   1                 |
---------------------------------------------------

FINAL SINGLE POINT ENERGY      -95.700000000

---------------------------------------------------
| Geometry Optimization Cycle   2                 |
---------------------------------------------------

FINAL SINGLE POINT ENERGY      -95.720000000
"""


# ---------------------------------------------------------------------------
# Parser integration tests
# ---------------------------------------------------------------------------


class TestParserRealisticOutputs:
    """Test parse_orca_output with realistic multi-section ORCA outputs."""

    def test_opt_freq_completed_full_extraction(self, tmp_path: Path) -> None:
        """B3LYP/6-31G(d) Opt Freq — all fields populated."""
        out = tmp_path / "formaldehyde_opt_freq.out"
        out.write_text(_B3LYP_OPT_FREQ_COMPLETED, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "completed"
        assert r.calc_type == "opt+freq"
        assert r.method == "B3LYP"
        assert r.basis_set == "6-31G(d)"
        assert r.charge == 0
        assert r.multiplicity == 1
        assert r.formula == "CH2O"
        assert r.n_atoms == 4
        assert r.energy_hartree == pytest.approx(-113.867432100)
        assert r.energy_ev is not None
        assert r.energy_kcalmol is not None
        assert r.opt_converged is True
        assert r.has_imaginary_freq is False
        assert r.lowest_freq_cm1 is not None
        assert r.lowest_freq_cm1 > 0
        assert r.enthalpy == pytest.approx(-113.834210)
        assert r.gibbs_energy == pytest.approx(-113.862100)
        assert r.wall_time_seconds == 2 * 3600 + 15 * 60 + 30
        assert r.file_hash != ""

    def test_dlpno_single_point(self, tmp_path: Path) -> None:
        """DLPNO-CCSD(T)/cc-pVTZ single point — no opt/freq data."""
        out = tmp_path / "ammonia_sp.out"
        out.write_text(_DLPNO_SP_COMPLETED, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "completed"
        assert r.calc_type == "sp"
        assert r.method == "DLPNO-CCSD(T)"
        assert r.basis_set == "cc-pVTZ"
        assert r.formula == "H3N"
        assert r.n_atoms == 4
        assert r.energy_hartree == pytest.approx(-56.520893412)
        assert r.opt_converged is None
        assert r.has_imaginary_freq is None
        assert r.enthalpy is None
        assert r.wall_time_seconds == 5 * 3600 + 42 * 60 + 18

    def test_ts_with_imaginary_frequency(self, tmp_path: Path) -> None:
        """OptTS with one imaginary frequency (expected for TS)."""
        out = tmp_path / "ts_sn2.out"
        out.write_text(_TS_OPT_WITH_IMAGINARY, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "completed"
        assert r.calc_type == "ts+freq"
        assert r.method == "B3LYP"
        assert r.basis_set == "def2-TZVP"
        assert r.formula == "CH4Cl"
        assert r.n_atoms == 6
        assert r.opt_converged is True
        assert r.has_imaginary_freq is True
        assert r.lowest_freq_cm1 == pytest.approx(-432.15)
        assert r.enthalpy == pytest.approx(-500.089123)
        assert r.gibbs_energy == pytest.approx(-500.112345)

    def test_scf_failure(self, tmp_path: Path) -> None:
        """SCF not converged → error termination → status=failed."""
        out = tmp_path / "fe_complex_scf_fail.out"
        out.write_text(_SCF_FAILED, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "failed"
        assert r.method == "wB97X-D3"
        assert r.basis_set == "def2-TZVP"
        assert r.charge == -1
        assert r.multiplicity == 2
        assert r.formula == "O4Fe"
        assert r.n_atoms == 5
        assert r.energy_hartree is None
        assert r.wall_time_seconds is None

    def test_opt_not_converged(self, tmp_path: Path) -> None:
        """Optimization did not converge but terminated normally → failed."""
        out = tmp_path / "ethane_opt_fail.out"
        out.write_text(_OPT_NOT_CONVERGED, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "failed"
        assert r.calc_type == "opt"
        assert r.method == "PBE0"
        assert r.basis_set == "def2-SVP"
        assert r.formula == "C2H6"
        assert r.n_atoms == 8
        assert r.opt_converged is False
        assert r.energy_hartree == pytest.approx(-79.65)
        assert r.wall_time_seconds == 12 * 3600

    def test_running_calculation(self, tmp_path: Path) -> None:
        """Incomplete output (no termination marker) → status=running."""
        out = tmp_path / "methylamine_running.out"
        out.write_text(_RUNNING_M06_2X, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "running"
        assert r.method == "M06-2X"
        assert r.basis_set == "6-311+G(d,p)"
        assert r.charge == 1
        assert r.multiplicity == 1
        assert r.formula == "CH6N"
        assert r.n_atoms == 8
        assert r.energy_hartree == pytest.approx(-95.72)
        assert r.wall_time_seconds is None

    def test_empty_file_returns_running(self, tmp_path: Path) -> None:
        """An empty output file (just started) → running status."""
        out = tmp_path / "empty.out"
        out.write_text("", encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.status == "running"
        assert r.method == ""
        assert r.energy_hartree is None

    def test_nonexistent_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_orca_output(str(tmp_path / "does_not_exist.out"))


# ---------------------------------------------------------------------------
# Full pipeline: parse → index → query
# ---------------------------------------------------------------------------


def _write_fixture(kb_dir: Path, name: str, content: str, status: str = "completed") -> Path:
    """Write an ORCA output and job_state.json into a job directory."""
    job_dir = kb_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "calc.out").write_text(content, encoding="utf-8")
    (job_dir / "job_state.json").write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=name,
                run_id=name,
                reaction_dir=str(job_dir),
                status=status,
                final_result={"status": status},
            )
        ),
        encoding="utf-8",
    )
    return job_dir


class TestParserIndexPipeline:
    """End-to-end: write realistic outputs → index → query/stats."""

    def test_index_multiple_calculations_and_query(self, tmp_path: Path) -> None:
        kb = tmp_path / "orca_runs"
        _write_fixture(kb, "formaldehyde", _B3LYP_OPT_FREQ_COMPLETED)
        _write_fixture(kb, "ammonia", _DLPNO_SP_COMPLETED)
        _write_fixture(kb, "ts_sn2", _TS_OPT_WITH_IMAGINARY)
        _write_fixture(kb, "ethane_fail", _OPT_NOT_CONVERGED, status="failed")

        index = DFTIndex()
        index.initialize(str(tmp_path / "dft.db"))

        result = index.index_calculations([str(kb)])
        assert result["indexed"] == 4
        assert result["failed"] == 0
        assert result["total"] == 4

        # Query by method
        b3lyp = index.query({"method": "B3LYP"})
        assert len(b3lyp) == 2  # formaldehyde + ts_sn2

        # Query by formula
        ch2o = index.search_by_formula("CH2O")
        assert len(ch2o) == 1
        assert ch2o[0]["energy_hartree"] == pytest.approx(-113.867432100)

        # Lowest energy
        lowest = index.get_lowest_energy(limit=2)
        assert len(lowest) == 2
        assert lowest[0]["energy_hartree"] < lowest[1]["energy_hartree"]

        # Stats
        stats = index.get_stats()
        assert stats["total"] == 4
        assert stats["by_status"].get("completed", 0) >= 2
        assert stats["by_status"].get("failed", 0) >= 1

        # Converged filter
        converged = index.query({"opt_converged": True})
        assert len(converged) == 2  # formaldehyde + ts_sn2

        not_converged = index.query({"opt_converged": False})
        assert len(not_converged) == 1  # ethane_fail

        # Imaginary freq filter
        with_imag = index.query({"has_imaginary_freq": True})
        assert len(with_imag) == 1
        assert with_imag[0]["formula"] == "CH4Cl"

        index.close()

    def test_incremental_reindex_skips_unchanged(self, tmp_path: Path) -> None:
        kb = tmp_path / "orca_runs"
        _write_fixture(kb, "job1", _B3LYP_OPT_FREQ_COMPLETED)

        index = DFTIndex()
        index.initialize(str(tmp_path / "dft.db"))

        r1 = index.index_calculations([str(kb)])
        assert r1["indexed"] == 1

        # Add a second job, re-index
        _write_fixture(kb, "job2", _DLPNO_SP_COMPLETED)
        r2 = index.index_calculations([str(kb)])
        assert r2["indexed"] == 1  # only new job
        assert r2["skipped"] == 1  # unchanged job
        assert r2["total"] == 2

        index.close()

    def test_monitor_detects_new_completed_calculation(self, tmp_path: Path) -> None:
        """DFTMonitor detects a newly completed calculation after baseline."""
        import os

        kb = tmp_path / "orca_runs"
        _write_fixture(kb, "job1", _B3LYP_OPT_FREQ_COMPLETED)

        index = DFTIndex()
        index.initialize(str(tmp_path / "dft.db"))
        state_file = str(tmp_path / "state.json")

        monitor = DFTMonitor(index, [str(kb)], state_file=state_file)

        # Baseline scan
        r1 = monitor.scan()
        assert r1.baseline_seeded is True
        assert r1.new_results == []

        # Simulate a completed calculation appearing
        job2 = _write_fixture(kb, "job2", _DLPNO_SP_COMPLETED)
        out_path = job2 / "calc.out"
        mtime = os.path.getmtime(out_path)
        os.utime(out_path, (mtime + 10, mtime + 10))

        r2 = monitor.scan()
        assert len(r2.new_results) == 1
        assert r2.new_results[0].status == "completed"
        assert r2.new_results[0].formula == "H3N"

        # Verify it was indexed
        results = index.query({"formula": "H3N"})
        assert len(results) == 1

        index.close()

    def test_comparison_query_across_methods(self, tmp_path: Path) -> None:
        """Test get_for_comparison with formula filter."""
        kb = tmp_path / "orca_runs"
        _write_fixture(kb, "opt_freq", _B3LYP_OPT_FREQ_COMPLETED)
        _write_fixture(kb, "ts", _TS_OPT_WITH_IMAGINARY)

        index = DFTIndex()
        index.initialize(str(tmp_path / "dft.db"))
        index.index_calculations([str(kb)])

        # Compare all B3LYP results sorted by energy
        b3lyp = index.get_for_comparison(method="B3LYP")
        assert len(b3lyp) == 2
        assert b3lyp[0]["energy_hartree"] <= b3lyp[1]["energy_hartree"]

        index.close()
