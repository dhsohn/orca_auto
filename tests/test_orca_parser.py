"""ORCA parser regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.parser import parse_opt_progress, parse_orca_output


def test_error_termination_is_classified_as_failed(tmp_path: Path) -> None:
    out_file = tmp_path / "error_case.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt",
                "* xyz 0 1",
                "C 0.0 0.0 0.0",
                "H 0.0 0.0 1.0",
                "*",
                "",
                "ORCA finished by error termination in SCF gradient",
                "[file orca_tools/qcmsg.cpp, line 394]:",
                "  .... aborting the run",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.status == "failed"


def test_utf16_completed_output_is_parsed(tmp_path: Path) -> None:
    out_file = tmp_path / "utf16_completed.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt",
                "* xyz 0 1",
                "C 0.0 0.0 0.0",
                "H 0.0 0.0 1.0",
                "*",
                "FINAL SINGLE POINT ENERGY      -100.123456",
                "                             ****ORCA TERMINATED NORMALLY****",
                "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
            ]
        ),
        encoding="utf-16",
    )

    result = parse_orca_output(str(out_file))

    assert result.status == "completed"
    assert result.method == "B3LYP"


def test_scants_route_is_classified_as_ts_freq(tmp_path: Path) -> None:
    out_file = tmp_path / "scants.out"
    out_file.write_text(
        "\n".join(
            [
                "! ScanTS B3LYP def2-SVP Freq",
                "* xyzfile 0 1 input.xyz",
                "FINAL SINGLE POINT ENERGY      -100.123456",
                "ORCA finished by error termination in Startup",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.calc_type == "ts+freq"
    assert result.status == "failed"


def test_parse_frequencies_uses_final_vibrational_frequency_block(tmp_path: Path) -> None:
    out_file = tmp_path / "multi_freq.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Freq",
                "VIBRATIONAL FREQUENCIES",
                "-----------------------",
                "  0:      -500.00 cm**-1",
                "  1:       120.00 cm**-1",
                "-----------------------",
                "VIBRATIONAL FREQUENCIES",
                "-----------------------",
                "  0:        -5.00 cm**-1",
                "  1:       130.00 cm**-1",
                "-----------------------",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.has_imaginary_freq is False
    assert result.lowest_freq_cm1 == pytest.approx(130.0)


# ---------------------------------------------------------------------------
# parse_opt_progress tests
# ---------------------------------------------------------------------------

_OPT_RUNNING_OUT = "\n".join(
    [
        "! B3LYP def2-SVP Opt",
        "* xyz 0 1",
        "C 0.0 0.0 0.0",
        "H 0.0 0.0 1.0",
        "*",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "----------------------------",
        " C    0.000000    0.000000    0.000000",
        " H    0.000000    0.000000    1.000000",
        "",
        "---------------------------------------------------",
        "| Geometry Optimization Cycle   1                 |",
        "---------------------------------------------------",
        "",
        "FINAL SINGLE POINT ENERGY      -100.100000000",
        "",
        "---------------------------------------------------",
        "| Geometry Optimization Cycle   2                 |",
        "---------------------------------------------------",
        "",
        "FINAL SINGLE POINT ENERGY      -100.120000000",
        "",
        "                         *************************************",
        "                         *  GEOMETRY CONVERGENCE              *",
        "                         *************************************",
        "Item                Value     Tolerance   Converged",
        "Energy change      -0.020000  5.0000e-06    NO",
        "MAX gradient        0.005000  3.0000e-04    NO",
        "RMS gradient        0.002000  1.0000e-04    NO",
        "MAX step            0.010000  4.0000e-03    NO",
        "RMS step            0.004000  2.0000e-03    NO",
        "",
        "---------------------------------------------------",
        "| Geometry Optimization Cycle   3                 |",
        "---------------------------------------------------",
        "",
        "FINAL SINGLE POINT ENERGY      -100.123000000",
        "",
        "                         *************************************",
        "                         *  GEOMETRY CONVERGENCE              *",
        "                         *************************************",
        "Item                Value     Tolerance   Converged",
        "Energy change      -0.003000  5.0000e-06    NO",
        "MAX gradient        0.000200  3.0000e-04    YES",
        "RMS gradient        0.000080  1.0000e-04    YES",
        "MAX step            0.003000  4.0000e-03    YES",
        "RMS step            0.001500  2.0000e-03    YES",
    ]
)


def test_parse_opt_progress_extracts_all_cycles(tmp_path: Path) -> None:
    out_file = tmp_path / "opt_running.out"
    out_file.write_text(_OPT_RUNNING_OUT, encoding="utf-8")

    progress = parse_opt_progress(str(out_file))

    assert len(progress.steps) == 3
    assert progress.formula == "CH"
    assert progress.method == "B3LYP"
    assert progress.basis_set == "def2-SVP"
    assert progress.calc_type == "opt"

    # Cycle 1: energy only, no convergence table
    assert progress.steps[0].cycle == 1
    assert progress.steps[0].energy_hartree == pytest.approx(-100.1)
    assert progress.steps[0].max_gradient is None

    # Cycle 2: energy + convergence table
    assert progress.steps[1].cycle == 2
    assert progress.steps[1].energy_hartree == pytest.approx(-100.12)
    assert progress.steps[1].energy_change == pytest.approx(-0.02)
    assert progress.steps[1].max_gradient == pytest.approx(0.005)
    assert progress.steps[1].converged_flags["MAX gradient"] is False

    # Cycle 3: partially converged
    assert progress.steps[2].cycle == 3
    assert progress.steps[2].max_gradient == pytest.approx(0.0002)
    assert progress.steps[2].converged_flags["MAX gradient"] is True
    assert sum(progress.steps[2].converged_flags.values()) == 4  # 4 out of 5 YES


def test_parse_opt_progress_accepts_uppercase_cycle_headers(tmp_path: Path) -> None:
    out_file = tmp_path / "opt_uppercase.out"
    out_file.write_text(
        _OPT_RUNNING_OUT.replace("Geometry Optimization Cycle", "GEOMETRY OPTIMIZATION CYCLE"),
        encoding="utf-8",
    )

    progress = parse_opt_progress(str(out_file))

    assert len(progress.steps) == 3
    assert progress.steps[-1].cycle == 3


def test_parse_opt_progress_running_detection(tmp_path: Path) -> None:
    """If ORCA TERMINATED NORMALLY is absent, is_running == True."""
    out_file = tmp_path / "running.out"
    out_file.write_text(_OPT_RUNNING_OUT, encoding="utf-8")

    progress = parse_opt_progress(str(out_file))
    assert progress.is_running is True
    assert progress.is_converged is False


def test_parse_opt_progress_converged_detection(tmp_path: Path) -> None:
    """When convergence is achieved and termination is normal, is_converged == True and is_running == False."""
    converged_out = _OPT_RUNNING_OUT + "\n".join(
        [
            "",
            "THE OPTIMIZATION HAS CONVERGED",
            "                             ****ORCA TERMINATED NORMALLY****",
            "TOTAL RUN TIME: 0 days 0 hours 5 minutes 30 seconds 0 msec",
        ]
    )
    out_file = tmp_path / "converged.out"
    out_file.write_text(converged_out, encoding="utf-8")

    progress = parse_opt_progress(str(out_file))
    assert progress.is_converged is True
    assert progress.is_running is False


def test_parse_opt_progress_sp_returns_empty_steps(tmp_path: Path) -> None:
    """Single-point calculations have no optimization cycles, so steps should be an empty list."""
    sp_out = "\n".join(
        [
            "! B3LYP def2-SVP",
            "* xyz 0 1",
            "C 0.0 0.0 0.0",
            "*",
            "FINAL SINGLE POINT ENERGY      -100.000000",
            "                             ****ORCA TERMINATED NORMALLY****",
            "TOTAL RUN TIME: 0 days 0 hours 0 minutes 10 seconds 0 msec",
        ]
    )
    out_file = tmp_path / "sp.out"
    out_file.write_text(sp_out, encoding="utf-8")

    progress = parse_opt_progress(str(out_file))
    assert progress.steps == []
    assert progress.is_running is False


# ---------------------------------------------------------------------------
# SI-oriented field tests
# ---------------------------------------------------------------------------


def test_parser_extracts_si_fields(tmp_path: Path) -> None:
    out_file = tmp_path / "si_fields.out"
    out_file.write_text(
        "\n".join(
            [
                "                                 Program Version 6.0.1 -  RELEASE  -",
                "|  1> ! wB97X-D3 def2-TZVP CPCM(toluene) OptTS Freq",
                "|  2> * xyz 0 1",
                "|  3> C 0.0 0.0 0.0",
                "|  4> *",
                "",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    1.234567   -0.987654",
                "  H      0.123456   -0.654321    2.000000",
                "",
                "FINAL SINGLE POINT ENERGY     -1234.567890123456",
                "--------------------------",
                "THERMOCHEMISTRY AT 298.15K",
                "--------------------------",
                "Zero point energy                ...      0.08843782 Eh      55.50 kcal/mol",
                "Total enthalpy                   ...  -1234.40000000 Eh",
                "Final Gibbs free energy          ...  -1234.45000000 Eh",
                "G-E(el)                          ...      0.11789012 Eh      73.98 kcal/mol",
                "",
                "                             ****ORCA TERMINATED NORMALLY****",
                "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.orca_version == "6.0.1"
    assert result.solvation == "CPCM(toluene)"
    assert result.zpe_correction == pytest.approx(0.08843782)
    assert result.gibbs_correction == pytest.approx(0.11789012)
    assert result.thermo_temperature_k == pytest.approx(298.15)
    assert result.coordinates == [
        ("C", 0.0, 1.234567, -0.987654),
        ("H", 0.123456, -0.654321, 2.0),
    ]


def test_parser_detects_smd_solvation(tmp_path: Path) -> None:
    out_file = tmp_path / "smd.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP CPCM",
                "|  2> %cpcm",
                "|  3>   smd true",
                '|  4>   SMDsolvent "water"',
                "|  5> end",
                "|  6> * xyz 0 1",
                "|  7> C 0.0 0.0 0.0",
                "|  8> *",
                "FINAL SINGLE POINT ENERGY      -100.000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.solvation == "SMD(water)"


def test_parser_reads_charge_multiplicity_from_xyzfile(tmp_path: Path) -> None:
    # Workflow-generated inputs use "* xyzfile <charge> <mult> <path>"; the
    # parser must read the real values, not fall back to Charge 0 / Mult 1.
    out_file = tmp_path / "xyzfile.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP Opt",
                "|  2> * xyzfile -1 2 conformer.xyz",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "FINAL SINGLE POINT ENERGY      -100.000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.charge == -1
    assert result.multiplicity == 2
