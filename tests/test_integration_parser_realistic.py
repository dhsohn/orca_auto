"""Integration tests: realistic ORCA outputs → parser and monitor.

Each fixture mirrors the structure of a real ORCA .out file with representative
sections (input line, coordinates, energy, convergence, frequencies, thermo,
termination, runtime).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.parser import parse_orca_output

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

# Real ORCA 5.x/6.x emits a "Scaling factor for frequencies" line and blank
# lines between the header rule and the numbered frequency list, then closes the
# block with a dashed "NORMAL MODES" header. This is the on-disk shape the parser
# must handle (the other fixtures use a simplified, blank-line-free layout).
_TS_REAL_VIB_FORMAT = """\
! B3LYP def2-TZVP OptTS Freq
* xyz 0 1
  C    0.000000    0.000000    0.000000
  H    1.089000    0.000000    0.000000
  Cl   3.000000    0.000000    0.000000
*

THE OPTIMIZATION HAS CONVERGED

-----------------------
VIBRATIONAL FREQUENCIES
-----------------------

Scaling factor for frequencies =  1.000000000  (already applied!)

   0:         0.00 cm**-1
   1:         0.00 cm**-1
   2:         0.00 cm**-1
   3:         0.00 cm**-1
   4:         0.00 cm**-1
   5:         0.00 cm**-1
   6:      -432.15 cm**-1
   7:       523.40 cm**-1
   8:      1450.78 cm**-1


------------
NORMAL MODES
------------

                             ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 0 hours 5 minutes 0 seconds 0 msec
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

    def test_real_orca_vibrational_section_with_scaling_factor(self, tmp_path: Path) -> None:
        """Real ORCA freq output separates the header rule from the numbered
        list with a 'Scaling factor' line and blank lines; the parser must still
        capture the frequencies. Regression: the section body previously
        terminated at the first blank line and dropped every frequency, so
        has_imaginary_freq/lowest_freq_cm1 came back None on real output."""
        out = tmp_path / "ts_real.out"
        out.write_text(_TS_REAL_VIB_FORMAT, encoding="utf-8")

        r = parse_orca_output(str(out))

        assert r.has_imaginary_freq is True
        assert r.lowest_freq_cm1 == pytest.approx(-432.15)

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
# Monitor end-to-end over realistic outputs
# ---------------------------------------------------------------------------
