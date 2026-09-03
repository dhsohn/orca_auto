"""ORCA parser regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.parser import parse_opt_progress, parse_orca_output


def test_annotated_final_energy_is_not_published(tmp_path: Path) -> None:
    # A near-converged SCF prints "(SCF not fully converged!)" on the final
    # energy line. That value must not populate the published energies, and an
    # earlier clean line belongs to a different geometry, so no energy at all
    # may be reported.
    out_file = tmp_path / "annotated.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "FINAL SINGLE POINT ENERGY      -100.123456789012 (SCF not fully converged!)",
                "--------------------------",
                "THERMOCHEMISTRY AT 298.15K",
                "--------------------------",
                "Zero point energy                ...      0.08843782 Eh",
                "Total enthalpy                   ...   -100.00000000 Eh",
                "Final Gibbs free energy          ...   -100.05000000 Eh",
                "G-E(el)                          ...      0.07345679 Eh",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.energy_hartree is None
    assert result.energy_ev is None
    assert result.energy_kcalmol is None
    # Thermochemistry derives from the same unconverged SCF: none of it may
    # be published either.
    assert result.enthalpy is None
    assert result.gibbs_energy is None
    assert result.zpe_correction is None
    assert result.gibbs_correction is None
    assert result.thermo_temperature_k is None


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
    assert result.electronic_state_verified is True


def test_parser_derives_gibbs_correction_when_line_absent(tmp_path: Path) -> None:
    # Some outputs print the final energy and Gibbs energy without a literal
    # "G-E(el)" line; both refer to the final geometry, so the correction is
    # exactly their difference — without it SP//opt composites silently vanish.
    out_file = tmp_path / "no_correction_line.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt Freq",
                "* xyz 0 1",
                "C 0.0 0.0 0.0",
                "*",
                "FINAL SINGLE POINT ENERGY      -100.500000000000",
                "--------------------------",
                "THERMOCHEMISTRY AT 298.15K",
                "--------------------------",
                "Total enthalpy                   ...  -100.40000000 Eh",
                "Final Gibbs free energy          ...  -100.38210988 Eh",
                "",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.gibbs_correction == pytest.approx(-100.38210988 - (-100.5))


def _thermochemistry_block(
    *,
    temperature: str,
    zpe: str,
    enthalpy: str,
    gibbs: str,
    correction: str,
    lowest_mode: str,
) -> list[str]:
    return [
        "-----------------------",
        "VIBRATIONAL FREQUENCIES",
        "-----------------------",
        "   0:         0.00 cm**-1",
        f"   6:      {lowest_mode} cm**-1",
        "--------------------------",
        f"THERMOCHEMISTRY AT {temperature}K",
        "--------------------------",
        f"Zero point energy                ...      {zpe} Eh",
        f"Total Enthalpy                   ...   {enthalpy} Eh",
        f"Total enthalpy                   ...   {enthalpy} Eh",
        f"Final Gibbs free energy          ...   {gibbs} Eh",
        f"G-E(el)                          ...      {correction} Eh",
    ]


_INITIAL_HESSIAN_BLOCK = _thermochemistry_block(
    temperature="298.15",
    zpe="0.05000000",
    enthalpy="-100.00000000",
    gibbs="-100.05000000",
    correction="0.05000000",
    lowest_mode="-650.00",
)
_FINAL_FREQ_BLOCK = _thermochemistry_block(
    temperature="350.00",
    zpe="0.06000000",
    enthalpy="-100.25000000",
    gibbs="-100.30000000",
    correction="-0.10000000",
    lowest_mode="-420.00",
)


def test_parser_binds_thermochemistry_to_the_final_energy_stage(tmp_path: Path) -> None:
    # Calc_Hess/Recalc_Hess print a full thermochemistry block for every
    # Hessian computed during the optimization. Only the block after the last
    # final single point energy describes the final geometry.
    out_file = tmp_path / "recalc_hess.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS Freq",
                "|  2> %geom Calc_Hess true Recalc_Hess 5 end",
                "FINAL SINGLE POINT ENERGY      -100.100000000000",
                *_INITIAL_HESSIAN_BLOCK,
                "FINAL SINGLE POINT ENERGY      -100.150000000000",
                "                    ***        THE OPTIMIZATION HAS CONVERGED      ***",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                *_FINAL_FREQ_BLOCK,
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.energy_hartree == pytest.approx(-100.2)
    assert result.zpe_correction == pytest.approx(0.06)
    assert result.enthalpy == pytest.approx(-100.25)
    assert result.gibbs_energy == pytest.approx(-100.30)
    assert result.gibbs_correction == pytest.approx(-0.10)
    assert result.thermo_temperature_k == pytest.approx(350.0)
    assert result.has_imaginary_freq is True
    assert result.lowest_freq_cm1 == pytest.approx(-420.0)


def test_parser_publishes_no_thermochemistry_when_the_final_stage_has_none(
    tmp_path: Path,
) -> None:
    # An OptTS without Freq still prints the initial Hessian's thermochemistry
    # before the optimization; that block belongs to the guess geometry, so
    # nothing may be attributed to the final one.
    out_file = tmp_path / "optts_no_freq.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS",
                "|  2> %geom Calc_Hess true end",
                *_INITIAL_HESSIAN_BLOCK,
                "FINAL SINGLE POINT ENERGY      -100.100000000000",
                "                    ***        THE OPTIMIZATION HAS CONVERGED      ***",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.energy_hartree == pytest.approx(-100.2)
    assert result.zpe_correction is None
    assert result.enthalpy is None
    assert result.gibbs_energy is None
    assert result.gibbs_correction is None
    assert result.thermo_temperature_k is None


@pytest.mark.parametrize(
    "final_energy_line",
    [None, "FINAL SINGLE POINT ENERGY      1.0D+400"],
    ids=["absent", "non-finite"],
)
def test_parser_publishes_no_thermochemistry_without_a_published_final_energy(
    tmp_path: Path,
    final_energy_line: str | None,
) -> None:
    out_file = tmp_path / "no_final_energy.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP Freq",
                *([final_energy_line] if final_energy_line else []),
                *_INITIAL_HESSIAN_BLOCK,
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output(str(out_file))

    assert result.energy_hartree is None
    assert result.gibbs_energy is None
    assert result.enthalpy is None
    assert result.zpe_correction is None
    assert result.gibbs_correction is None
    assert result.thermo_temperature_k is None


def test_final_energy_pattern_is_line_anchored_and_parses_d_exponent() -> None:
    from orca_auto.orca.parser.patterns import (
        FINAL_SINGLE_POINT_ENERGY_BYTES_RE,
        FINAL_SINGLE_POINT_ENERGY_RE,
        final_single_point_energy_value,
    )

    text = (
        "note: FINAL SINGLE POINT ENERGY -1.0 mentioned mid-line\n"
        "FINAL SINGLE POINT ENERGY   -76.123456789012\r\n"
        "FINAL SINGLE POINT ENERGY   1.2.3\n"
        "FINAL SINGLE POINT ENERGY   -7.5D-01\n"
        "FINAL SINGLE POINT ENERGY  -137.654063943692   (SCF not fully converged!)\n"
    )

    matches = list(FINAL_SINGLE_POINT_ENERGY_RE.finditer(text))
    values = [final_single_point_energy_value(match.group(1)) for match in matches]

    # The mid-line phrase and the malformed number never match; \r\n line
    # endings and the Fortran D exponent parse; the real ORCA near-converged
    # annotation line still yields its value, with the annotation captured
    # separately so consumers can reject it.
    assert values == [
        pytest.approx(-76.123456789012),
        pytest.approx(-0.75),
        pytest.approx(-137.654063943692),
    ]
    assert [match.group(2) for match in matches] == [
        None,
        None,
        "(SCF not fully converged!)",
    ]

    byte_values = [
        final_single_point_energy_value(match.group(1))
        for match in FINAL_SINGLE_POINT_ENERGY_BYTES_RE.finditer(text.encode("ascii"))
    ]
    assert byte_values == values

    with pytest.raises(ValueError, match="non-finite"):
        final_single_point_energy_value("1E999")
