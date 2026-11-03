"""Integration tests: realistic ORCA outputs → parser, frequency analysis, analyzer.

Each fixture mirrors the structure of a real ORCA .out file with representative
sections (input line, coordinates, energy, convergence, frequencies, thermo,
termination, runtime).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.evidence import parsed_frequency_analysis
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.parser import parse_orca_output_text
from orca_auto.orca.parser.io import read_orca_text
from tests.orca_output_helpers import (
    B3LYP_OPT_FREQ_COMPLETED_OUT,
    TS_OPT_WITH_IMAGINARY_OUT,
    TS_REAL_VIB_FORMAT_OUT,
)

# ---------------------------------------------------------------------------
# Realistic ORCA output fixtures
# ---------------------------------------------------------------------------


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


# Real ORCA 5.x/6.x emits a "Scaling factor for frequencies" line and blank
# lines between the header rule and the numbered frequency list, then closes the
# block with a dashed "NORMAL MODES" header. This is the on-disk shape the parser
# must handle (the other fixtures use a simplified, blank-line-free layout).

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
    """Test parse_orca_output_text with realistic multi-section ORCA outputs."""

    def test_opt_freq_completed_full_extraction(self, tmp_path: Path) -> None:
        """B3LYP/6-31G(d) Opt Freq — all fields populated."""
        out = tmp_path / "formaldehyde_opt_freq.out"
        out.write_text(B3LYP_OPT_FREQ_COMPLETED_OUT, encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

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
        assert r.enthalpy == pytest.approx(-113.834210)
        assert r.gibbs_energy == pytest.approx(-113.862100)
        assert r.wall_time_seconds == 2 * 3600 + 15 * 60 + 30
        analysis = parsed_frequency_analysis(out)
        assert analysis is not None
        assert analysis.imaginary_count() == 0
        assert min(f for f in analysis.frequencies if f != 0.0) == pytest.approx(1167.32)

    def test_dlpno_single_point(self, tmp_path: Path) -> None:
        """DLPNO-CCSD(T)/cc-pVTZ single point — no opt/freq data."""
        out = tmp_path / "ammonia_sp.out"
        out.write_text(_DLPNO_SP_COMPLETED, encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

        assert r.method == "DLPNO-CCSD(T)"
        assert r.basis_set == "cc-pVTZ"
        assert r.formula == "H3N"
        assert r.n_atoms == 4
        assert r.energy_hartree == pytest.approx(-56.520893412)
        assert r.opt_converged is None
        assert r.enthalpy is None
        assert r.wall_time_seconds == 5 * 3600 + 42 * 60 + 18
        assert parsed_frequency_analysis(out) is None

    def test_ts_with_imaginary_frequency(self, tmp_path: Path) -> None:
        """OptTS with one imaginary frequency (expected for TS)."""
        out = tmp_path / "ts_sn2.out"
        out.write_text(TS_OPT_WITH_IMAGINARY_OUT, encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

        assert r.method == "B3LYP"
        assert r.basis_set == "def2-TZVP"
        assert r.formula == "CH4Cl"
        assert r.n_atoms == 6
        assert r.opt_converged is True
        assert r.enthalpy == pytest.approx(-500.089123)
        assert r.gibbs_energy == pytest.approx(-500.112345)
        analysis = parsed_frequency_analysis(out)
        assert analysis is not None
        assert analysis.imaginary_count() == 1
        assert min(analysis.frequencies) == pytest.approx(-432.15)

    def test_real_orca_vibrational_section_with_scaling_factor(self, tmp_path: Path) -> None:
        """Real ORCA freq output separates the header rule from the numbered
        list with a 'Scaling factor' line and blank lines; the frequency
        analysis must still capture the frequencies. Regression: a blank-line
        terminated section body once dropped every frequency on real output."""
        out = tmp_path / "ts_real.out"
        out.write_text(TS_REAL_VIB_FORMAT_OUT, encoding="utf-8")

        analysis = parsed_frequency_analysis(out)

        assert analysis is not None
        assert analysis.imaginary_count() == 1
        assert min(analysis.frequencies) == pytest.approx(-432.15)
        assert analyze_output(out, CompletionMode("ts", False)).status == "completed"

    def test_scf_failure(self, tmp_path: Path) -> None:
        """SCF not converged → error termination → SCF-gradient abort verdict."""
        out = tmp_path / "fe_complex_scf_fail.out"
        out.write_text(_SCF_FAILED, encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

        assert analyze_output(out, CompletionMode("opt", False)).status == ("error_scfgrad_abort")
        assert r.method == "wB97X-D3"
        assert r.basis_set == "def2-TZVP"
        assert r.charge == -1
        assert r.multiplicity == 2
        assert r.formula == "FeO4"
        assert r.n_atoms == 5
        assert r.energy_hartree is None
        assert r.wall_time_seconds is None

    def test_opt_not_converged(self, tmp_path: Path) -> None:
        """Optimization did not converge but terminated normally → not completed."""
        out = tmp_path / "ethane_opt_fail.out"
        out.write_text(_OPT_NOT_CONVERGED, encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

        assert analyze_output(out, CompletionMode("opt", False)).status == ("geom_not_converged")
        assert r.method == "PBE0"
        assert r.basis_set == "def2-SVP"
        assert r.formula == "C2H6"
        assert r.n_atoms == 8
        assert r.opt_converged is False
        assert r.energy_hartree == pytest.approx(-79.65)
        assert r.wall_time_seconds == 12 * 3600

    def test_running_calculation(self, tmp_path: Path) -> None:
        """Incomplete output (no termination marker) → incomplete verdict."""
        out = tmp_path / "methylamine_running.out"
        out.write_text(_RUNNING_M06_2X, encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

        assert analyze_output(out, CompletionMode("opt", False)).status == "incomplete"
        assert r.method == "M06-2X"
        assert r.basis_set == "6-311+G(d,p)"
        assert r.charge == 1
        assert r.multiplicity == 1
        assert r.formula == "CH6N"
        assert r.n_atoms == 8
        assert r.energy_hartree == pytest.approx(-95.72)
        assert r.wall_time_seconds is None

    def test_empty_file_is_incomplete(self, tmp_path: Path) -> None:
        """An empty output file (just started) → incomplete verdict, no metadata."""
        out = tmp_path / "empty.out"
        out.write_text("", encoding="utf-8")

        r = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))

        assert analyze_output(out, CompletionMode("opt", False)).status == "incomplete"
        assert r.method == ""
        assert r.energy_hartree is None

    def test_nonexistent_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_orca_output_text(
                read_orca_text(str(tmp_path / "does_not_exist.out")),
                source_path=str(tmp_path / "does_not_exist.out"),
            )
