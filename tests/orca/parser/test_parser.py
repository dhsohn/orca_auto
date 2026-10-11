"""ORCA parser regression tests."""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.evidence import parsed_frequency_analysis
from orca_auto.orca.orca_opt_progress import parse_opt_progress_text
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.output_status import last_optimization_convergence
from orca_auto.orca.parser import parse_orca_output_text
from orca_auto.orca.parser.io import read_orca_text
from orca_auto.orca.report.composer import collect_html_report_parts


def test_optimization_verdict_absence_and_same_line_negative_precedence() -> None:
    assert last_optimization_convergence(["no verdict here"]) is None
    assert (
        last_optimization_convergence(
            ["THE OPTIMIZATION HAS CONVERGED; THE OPTIMIZATION DID NOT CONVERGE"]
        )
        is False
    )


@pytest.mark.parametrize(
    "positive,negative",
    [
        ("THE OPTIMIZATION HAS CONVERGED", "THE OPTIMIZATION DID NOT CONVERGE"),
        ("OPTIMIZATION RUN DONE", "OPTIMIZATION HAS NOT YET CONVERGED"),
        ("the optimization has converged", "ORCA GEOMETRY OPTIMIZATION NOT CONVERGED"),
    ],
)
@pytest.mark.parametrize("converged", [False, True])
@pytest.mark.parametrize("padding", ["", "padding\n" * 40000], ids=["small", "tail-fallback"])
def test_last_optimization_verdict_agrees_across_consumers(
    tmp_path: Path, positive: str, negative: str, converged: bool, padding: str
) -> None:
    first, last = (negative, positive) if converged else (positive, negative)
    out = tmp_path / "optimization.out"
    out.write_text(
        "! HF STO-3G Opt\n"
        f"GEOMETRY OPTIMIZATION CYCLE 1\nFINAL SINGLE POINT ENERGY -1.0\n{first}\n"
        f"GEOMETRY OPTIMIZATION CYCLE 2\nFINAL SINGLE POINT ENERGY -0.9\n{last}\n"
        f"{padding}****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )
    analysis = analyze_output(out, CompletionMode("opt", False))
    result = parse_orca_output_text(read_orca_text(str(out)), source_path=str(out))
    progress = parse_opt_progress_text(read_orca_text(str(out)), source_path=str(out))
    inp = tmp_path / "optimization.inp"
    inp.write_text("! HF STO-3G Opt\n", encoding="utf-8")
    parts = collect_html_report_parts(
        tmp_path, {"selected_inp": str(inp), "attempts": [{"out_path": str(out)}]}
    )

    assert analysis.markers["last_opt_converged"] is converged
    assert analysis.status.value == ("completed" if converged else "geom_not_converged")
    assert result.opt_converged is converged
    assert progress.is_converged is converged
    assert len(progress.steps) == 2
    assert parts is not None and parts.opt is not None
    assert parts.opt.opt_converged is converged


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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.method == "B3LYP"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_parse_orca_output_reads_output_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    out_file = tmp_path / "single_read.out"
    out_file.write_text(
        "! B3LYP def2-SVP SP\n"
        "FINAL SINGLE POINT ENERGY -100.123456\n"
        "****ORCA TERMINATED NORMALLY****\n",
        encoding=encoding,
    )
    original_open = builtins.open
    output_opens = 0

    def tracked_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal output_opens
        if file == str(out_file) or file == out_file:
            output_opens += 1
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.method == "B3LYP"
    assert result.energy_hartree == pytest.approx(-100.123456)
    assert output_opens == 1


def test_frequency_analysis_uses_final_vibrational_frequency_block(tmp_path: Path) -> None:
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

    analysis = parsed_frequency_analysis(out_file)

    assert analysis is not None
    assert analysis.frequencies == pytest.approx((-5.0, 130.0))
    assert analysis.imaginary_count() == 0


@pytest.mark.parametrize(
    ("rows", "symbols"),
    [
        (
            [
                "  C      0.000000    0.000000    0.000000",
                "  DA     0.500000    0.000000    0.000000",
                "  H      1.000000    0.000000    0.000000",
            ],
            ("C", "DA", "H"),
        ),
        (
            [
                "  c      0.000000    0.000000    0.000000",
                "  H      1.000000    0.000000    0.000000",
            ],
            ("c", "H"),
        ),
        (
            [
                "  C      0.000000    0.000000    0.000000  1",
                "  H      1.000000    0.000000    0.000000  2",
            ],
            (),
        ),
        (["  C      0    0    0", "  H      1    0    0"], ()),
    ],
    ids=["dummy_atom", "lower_case_symbol", "extra_column", "integer_coordinates"],
)
def test_frequency_geometry_keeps_its_own_coordinate_row_rule(
    tmp_path: Path, rows: list[str], symbols: tuple[str, ...]
) -> None:
    # Unlike the result parser's coordinate rows: any one- or two-letter
    # symbol, but decimal xyz values and nothing after z.
    out_file = tmp_path / "coords.out"
    out_file.write_text(
        "\n".join(
            [
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                *rows,
                "",
                "FINAL SINGLE POINT ENERGY      -100.10",
                "VIBRATIONAL FREQUENCIES",
                "   0:         0.00 cm**-1",
                "   1:      -350.00 cm**-1 ***imaginary mode***",
                "",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    analysis = parsed_frequency_analysis(out_file)

    assert analysis is not None
    assert tuple(atom[0] for atom in analysis.atoms) == symbols


# ---------------------------------------------------------------------------
# parse_opt_progress_text tests
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

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert len(progress.steps) == 3
    assert progress.formula == "CH"
    assert progress.method == "B3LYP"
    assert progress.basis_set == "def2-SVP"

    # Cycle 1: energy only, no convergence table
    assert progress.steps[0].cycle == 1
    assert progress.steps[0].energy_hartree == pytest.approx(-100.1)

    # Cycle 2: energy + convergence table
    assert progress.steps[1].cycle == 2
    assert progress.steps[1].energy_hartree == pytest.approx(-100.12)

    # Cycle 3: energy remains available even with a partially converged table
    assert progress.steps[2].cycle == 3
    assert progress.steps[2].energy_hartree == pytest.approx(-100.123)
    assert progress.is_converged is False


def test_parse_opt_progress_accepts_uppercase_cycle_headers(tmp_path: Path) -> None:
    out_file = tmp_path / "opt_uppercase.out"
    out_file.write_text(
        _OPT_RUNNING_OUT.replace("Geometry Optimization Cycle", "GEOMETRY OPTIMIZATION CYCLE"),
        encoding="utf-8",
    )

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert len(progress.steps) == 3
    assert progress.steps[-1].cycle == 3


def test_parse_opt_progress_keeps_unfinished_energy_steps(tmp_path: Path) -> None:
    out_file = tmp_path / "running.out"
    out_file.write_text(_OPT_RUNNING_OUT, encoding="utf-8")

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))
    assert [step.cycle for step in progress.steps] == [1, 2, 3]
    assert progress.steps[-1].energy_hartree == pytest.approx(-100.123)
    assert progress.is_converged is False


def test_parse_opt_progress_converged_detection(tmp_path: Path) -> None:
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

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))
    assert progress.is_converged is True


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

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))
    assert progress.steps == []
    assert progress.is_converged is False


@pytest.mark.parametrize(
    "header",
    ["", "Geometry Optimization Cycle 1\n"],
    ids=["no-cycle", "cycle-without-finite-energy"],
)
def test_parse_opt_progress_without_finite_cycles_returns_no_steps(
    tmp_path: Path, header: str
) -> None:
    out_file = tmp_path / "unfinished.out"
    out_file.write_text(header + "FINAL SINGLE POINT ENERGY 1E999\n", encoding="utf-8")

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert progress.steps == []
    assert progress.is_converged is False


def test_parse_opt_progress_keeps_last_finite_energy_per_cycle(tmp_path: Path) -> None:
    out_file = tmp_path / "cycles.out"
    out_file.write_text(
        "FINAL SINGLE POINT ENERGY -99\n"
        "Geometry Optimization Cycle 3\n"
        "FINAL SINGLE POINT ENERGY -2\n"
        "FINAL SINGLE POINT ENERGY -3D0 (SCF not fully converged!)\n"
        "FINAL SINGLE POINT ENERGY -1E999\n"
        "MAX gradient 0.2 0.01 NO\n"
        "MAX gradient 0.1 0.01 YES\n"
        "Geometry Optimization Cycle 9\n"
        "FINAL SINGLE POINT ENERGY NaN\n"
        "MAX gradient 0.9 0.01 NO\n"
        "GEOMETRY OPTIMIZATION CYCLE 3\n"
        "FINAL SINGLE POINT ENERGY -4.5d+0\n"
        "FINAL SINGLE POINT ENERGY -1.2.3\n"
        "prefix FINAL SINGLE POINT ENERGY -10\n"
        "Geometry Optimization Cycle 1\n"
        "FINAL SINGLE POINT ENERGY -5\n",
        encoding="utf-8",
    )

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert [(step.cycle, step.energy_hartree) for step in progress.steps] == [
        (3, -3.0),
        (3, -4.5),
        (1, -5.0),
    ]
    assert progress.is_converged is False


def test_parse_opt_progress_reads_energy_despite_malformed_convergence_table(
    tmp_path: Path,
) -> None:
    out_file = tmp_path / "malformed_table.out"
    out_file.write_text(
        "Geometry Optimization Cycle 1\n"
        "FINAL SINGLE POINT ENERGY -1\n"
        "MAX gradient 1.2.3 0.01 NO\n"
        "THE OPTIMIZATION HAS CONVERGED\n",
        encoding="utf-8",
    )

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert [(step.cycle, step.energy_hartree) for step in progress.steps] == [(1, -1.0)]
    assert progress.is_converged is True


def test_parse_opt_progress_assigns_whole_text_energy_matches_by_start(tmp_path: Path) -> None:
    out_file = tmp_path / "cycle_boundary.out"
    out_file.write_text(
        "Geometry Optimization Cycle 1\n"
        "FINAL SINGLE POINT ENERGY -1 (Geometry Optimization Cycle 2)\n"
        "FINAL SINGLE POINT ENERGY -2\n",
        encoding="utf-8",
    )

    progress = parse_opt_progress_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert [(step.cycle, step.energy_hartree) for step in progress.steps] == [(1, -1.0), (2, -2.0)]


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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.orca_version == "6.0.1"
    assert result.solvation == "CPCM(toluene)"
    assert result.zpe_correction == pytest.approx(0.08843782)
    assert result.gibbs_correction == pytest.approx(0.11789012)
    assert result.thermo_temperature_k == pytest.approx(298.15)
    assert result.coordinates == [
        ("C", 0.0, 1.234567, -0.987654),
        ("H", 0.123456, -0.654321, 2.0),
    ]


def test_parser_ignores_echoed_route_comment_for_report_metadata(tmp_path: Path) -> None:
    out_file = tmp_path / "commented_route.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP CPCM(toluene) # CCSD(T) def2-TZVP smd true",
                "|  2> * xyz 0 1",
                "|  3> C 0.0 0.0 0.0",
                "|  4> *",
                "FINAL SINGLE POINT ENERGY      -100.000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.method == "B3LYP"
    assert result.basis_set == "def2-SVP"
    assert result.solvation == "CPCM(toluene)"


@pytest.mark.parametrize(
    "route",
    [
        "B3LYP # CCSD(T) def2-TZVP smd true # def2-SVP CPCM(toluene)",
        "B3LYP# CCSD(T) def2-TZVP #def2-SVP# smd true #CPCM(toluene)# MP2 def2-QZVP",
    ],
)
def test_parser_preserves_route_metadata_after_closed_comments(tmp_path: Path, route: str) -> None:
    out_file = tmp_path / "closed_route_comments.out"
    out_file.write_text(
        "\n".join(
            [
                f"|  1> ! {route}",
                "FINAL SINGLE POINT ENERGY      -100.000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.method == "B3LYP"
    assert result.basis_set == "def2-SVP"
    assert result.solvation == "CPCM(toluene)"
    assert result.input_line == "B3LYP def2-SVP CPCM(toluene)"


@pytest.mark.parametrize("solvent", ["water", "water#quoted"])
def test_parser_preserves_smd_directives_after_closed_comments(
    tmp_path: Path, solvent: str
) -> None:
    out_file = tmp_path / "closed_smd_comments.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP CPCM(toluene)",
                "|  2> %cpcm",
                "|  3>   # smd false # smd # ignored # true # smd false",
                f'|  4>   # SMDsolvent "hexane" # SMDsolvent "{solvent}" # SMDsolvent "wrong"',
                "|  5> end",
                "FINAL SINGLE POINT ENERGY      -100.000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.solvation == f"SMD({solvent})"


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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.solvation == "SMD(water)"


@pytest.mark.parametrize("geometry", ["xyz", "XYZ", "xyzfile", "XyZFiLe"])
@pytest.mark.parametrize("prefix", ["", "|  2> "])
def test_parser_reads_charge_multiplicity_from_geometry(
    tmp_path: Path, geometry: str, prefix: str
) -> None:
    # Workflow-generated inputs use "* xyzfile <charge> <mult> <path>"; the
    # parser must read the real values, not fall back to Charge 0 / Mult 1.
    out_file = tmp_path / "xyzfile.out"
    out_file.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP Opt",
                "|  1> # previous input: * xyz 0 1",
                f"{prefix}* {geometry} -1 2"
                + (" geometry.xyz" if geometry.lower() == "xyzfile" else ""),
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "FINAL SINGLE POINT ENERGY      -100.000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.charge == -1
    assert result.multiplicity == 2
    assert result.electronic_state_verified is True


def test_parser_does_not_verify_electronic_state_from_a_commented_geometry(tmp_path: Path) -> None:
    out_file = tmp_path / "commented_geometry.out"
    out_file.write_text("| 2> # old geometry: * xyz -1 2\nORCA TERMINATED NORMALLY\n")

    assert (
        parse_orca_output_text(
            read_orca_text(str(out_file)), source_path=str(out_file)
        ).electronic_state_verified
        is False
    )


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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.energy_hartree == pytest.approx(-100.2)
    assert result.zpe_correction == pytest.approx(0.06)
    assert result.enthalpy == pytest.approx(-100.25)
    assert result.gibbs_energy == pytest.approx(-100.30)
    assert result.gibbs_correction == pytest.approx(-0.10)
    assert result.thermo_temperature_k == pytest.approx(350.0)
    analysis = parsed_frequency_analysis(out_file)
    assert analysis is not None
    assert analysis.frequencies == pytest.approx((0.0, -420.0))
    assert analysis.imaginary_count() == 1


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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

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

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.energy_hartree is None
    assert result.gibbs_energy is None
    assert result.enthalpy is None
    assert result.zpe_correction is None
    assert result.gibbs_correction is None
    assert result.thermo_temperature_k is None


def test_final_energy_pattern_is_line_anchored_and_parses_d_exponent() -> None:
    from orca_auto.orca.parser.patterns import (
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

    with pytest.raises(ValueError, match="non-finite"):
        final_single_point_energy_value("1E999")


def test_error_banner_is_not_parsed_as_a_route_line(tmp_path: Path) -> None:
    # ORCA prints "!!!" rules around a fatal error; the route line is the one
    # that starts with a single "!".
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS Freq",
                "|  2> * xyzfile 0 1 input.xyz",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                "!!!                        FATAL ERROR ENCOUNTERED               !!!",
                "!!!                        -----------------------               !!!",
                "!!!            I/O OPERATION FAILED                              !!!",
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_path)), source_path=str(out_path))

    assert result.input_line == "B3LYP def2-SVP OptTS Freq"
    assert "FATAL" not in result.input_line


def test_route_line_without_a_prompt_is_still_parsed(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    out_path.write_text("! Opt B3LYP def2-SVP\n!Freq\n", encoding="utf-8")

    result = parse_orca_output_text(read_orca_text(str(out_path)), source_path=str(out_path))

    assert result.input_line == "Opt B3LYP def2-SVP Freq"


def test_opt_progress_module_imports_in_a_fresh_interpreter() -> None:
    """The parser package must not re-export from a module that imports the parser."""
    completed = subprocess.run(
        [sys.executable, "-c", "import orca_auto.orca.orca_opt_progress"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("final_rows", "out_name"),
    [
        ([], "empty_final_coords.out"),
        (
            ["  C      0.500000    0.000000    0.000000", "  H      BROKEN"],
            "truncated_final_coords.out",
        ),
    ],
    ids=["empty", "truncated"],
)
def test_parser_fails_closed_on_empty_or_truncated_final_coordinates(
    tmp_path: Path, final_rows: list[str], out_name: str
) -> None:
    out_file = tmp_path / out_name
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt",
                "* xyz 0 1",
                "C 0.0 0.0 0.0",
                "*",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "  H      1.000000    0.000000    0.000000",
                "FINAL SINGLE POINT ENERGY      -100.100000",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                *final_rows,
                "",
                "FINAL SINGLE POINT ENERGY      -100.200000",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.energy_hartree == pytest.approx(-100.2)
    assert result.coordinates == []
    assert result.elements == []
    assert result.n_atoms == 0
    assert result.formula == ""


@pytest.mark.parametrize(
    ("final_tail", "expected_energy", "include_termination", "out_name"),
    [
        (
            ["", "FINAL SINGLE POINT ENERGY      -100.200000"],
            -100.2,
            True,
            "row_boundary_then_energy.out",
        ),
        ([], -100.1, False, "row_boundary_eof.out"),
    ],
    ids=["blank_then_energy", "eof_after_complete_row"],
)
def test_parser_fails_closed_on_row_boundary_truncation_in_final_coordinates(
    tmp_path: Path,
    final_tail: list[str],
    expected_energy: float,
    include_termination: bool,
    out_name: str,
) -> None:
    out_file = tmp_path / out_name
    lines = [
        "! B3LYP def2-SVP Opt",
        "* xyz 0 1",
        "C 0.0 0.0 0.0",
        "*",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
        "  C      0.000000    0.000000    0.000000",
        "  H      1.000000    0.000000    0.000000",
        "FINAL SINGLE POINT ENERGY      -100.100000",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
        "  C      0.500000    0.000000    0.000000",
        *final_tail,
    ]
    if include_termination:
        lines.append("****ORCA TERMINATED NORMALLY****")
    out_file.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.energy_hartree == pytest.approx(expected_energy)
    assert result.coordinates == []
    assert result.elements == []
    assert result.n_atoms == 0
    assert result.formula == ""


def test_parser_keeps_complete_final_coordinate_replacement(tmp_path: Path) -> None:
    out_file = tmp_path / "complete_final_coords.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt",
                "* xyz 0 1",
                "C 0.0 0.0 0.0",
                "*",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "  H      1.000000    0.000000    0.000000",
                "FINAL SINGLE POINT ENERGY      -100.100000",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.500000    0.000000    0.000000",
                "  H      1.500000    0.000000    0.000000",
                "",
                "FINAL SINGLE POINT ENERGY      -100.200000",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.energy_hartree == pytest.approx(-100.2)
    assert result.coordinates == [("C", 0.5, 0.0, 0.0), ("H", 1.5, 0.0, 0.0)]
    assert result.elements == ["C", "H"]
    assert result.n_atoms == 2
    assert result.formula == "CH"


@pytest.mark.parametrize(
    "malformed_row",
    [
        "  1      1.000000    0.000000    0.000000",
        "  H1     1.000000    0.000000    0.000000",
        "  Xyz    1.000000    0.000000    0.000000",
        "  ****   1.000000    0.000000    0.000000",
        "  FINAL SINGLE POINT ENERGY      BROKEN",
    ],
    ids=["numeric_symbol", "labelled_symbol", "long_symbol", "non_alpha_symbol", "broken_energy"],
)
def test_parser_fails_closed_on_malformed_row_inside_coordinates(
    tmp_path: Path, malformed_row: str
) -> None:
    # A non-row line inside the table is not a section end: the valid rows
    # before it must not be published as a partial geometry.
    out_file = tmp_path / "malformed_row.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP",
                "* xyz 0 1",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                malformed_row,
                "  H      2.000000    0.000000    0.000000",
                "",
                "FINAL SINGLE POINT ENERGY      -100.100000",
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.energy_hartree == pytest.approx(-100.1)
    assert result.coordinates == []
    assert result.elements == []
    assert result.n_atoms == 0
    assert result.formula == ""


@pytest.mark.parametrize(
    ("earlier_table", "final_rows", "energy_line", "expected"),
    [
        (
            [],
            ["  C      0.500000    0.000000    0.000000"],
            "FINAL SINGLE POINT ENERGY      -100.200000",
            [("C", 0.5, 0.0, 0.0)],
        ),
        (
            [],
            ["  C      0.500000    0.000000    0.000000"],
            "FINAL SINGLE POINT ENERGY      -100.200000 (SCF not fully converged!)",
            [("C", 0.5, 0.0, 0.0)],
        ),
        (
            [
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "  H      1.000000    0.000000    0.000000",
                "FINAL SINGLE POINT ENERGY      -100.100000",
            ],
            [
                "  C      0.500000    0.000000    0.000000",
                "  H      1.500000    0.000000    0.000000",
            ],
            "FINAL SINGLE POINT ENERGY      -100.200000",
            [("C", 0.5, 0.0, 0.0), ("H", 1.5, 0.0, 0.0)],
        ),
        (
            [
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "  H      1.000000    0.000000    0.000000",
                "FINAL SINGLE POINT ENERGY      -100.100000",
            ],
            ["  C      0.500000    0.000000    0.000000"],
            "FINAL SINGLE POINT ENERGY      -100.200000",
            [],
        ),
    ],
    ids=["single_row", "annotated_energy", "complete_replacement", "shortened_replacement"],
)
def test_parser_ends_coordinates_at_final_energy_line_without_blank(
    tmp_path: Path,
    earlier_table: list[str],
    final_rows: list[str],
    energy_line: str,
    expected: list[tuple[str, float, float, float]],
) -> None:
    out_file = tmp_path / "no_blank_termination.out"
    out_file.write_text(
        "\n".join(
            [
                "! B3LYP def2-SVP Opt",
                "* xyz 0 1",
                *earlier_table,
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                *final_rows,
                energy_line,
                "****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_orca_output_text(read_orca_text(str(out_file)), source_path=str(out_file))

    assert result.coordinates == expected
    assert result.n_atoms == len(expected)


@pytest.mark.parametrize("damage", ["empty", "malformed", "row_boundary"])
def test_parser_rejects_damaged_final_table_in_authentic_water_output(damage: str) -> None:
    # Start from retained ORCA 6.1.1 acceptance bytes (see provenance.json).
    # Only the final Angstroem table is damaged; its surrounding output and
    # final energy remain intact. These mutations are corruption probes,
    # not claims that the engine emitted these damaged outputs.
    fixture = Path(__file__).parents[2] / "fixtures" / "orca_6_1_1" / "water_opt_freq.out"
    text = read_orca_text(str(fixture))
    header = "CARTESIAN COORDINATES (ANGSTROEM)"
    prefix, final_section = text.rsplit(header, 1)
    lines = final_section.splitlines(keepends=True)
    assert lines[2:5] == [
        "  O      0.066935    0.000000    0.066935\n",
        "  H     -0.019526    0.000000    1.052591\n",
        "  H      1.052591    0.000000   -0.019526\n",
    ]
    if damage == "empty":
        del lines[2:5]
    elif damage == "malformed":
        lines[3] = "  H      BROKEN\n"
    else:
        del lines[4]
    result = parse_orca_output_text(prefix + header + "".join(lines), source_path=str(fixture))

    assert result.energy_hartree == pytest.approx(-74.965901189921, abs=1e-10)
    assert result.coordinates == []
    assert result.elements == []
    assert result.n_atoms == 0
    assert result.formula == ""


@pytest.mark.parametrize(
    ("counts", "expected_count"),
    [([3, 2, 2], 0), ([3, 2, 3], 3)],
    ids=["repeated_truncation", "complete_final_after_truncation"],
)
def test_final_coordinates_keep_first_established_atom_count(
    counts: list[int], expected_count: int
) -> None:
    rows = ["O 0.0 0.0 0.0", "H 1.0 0.0 0.0", "H 0.0 1.0 0.0"]
    text = "".join(
        "CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
        + "\n".join(rows[:count])
        + f"\n\nFINAL SINGLE POINT ENERGY {-75.0 - index}\n"
        for index, count in enumerate(counts)
    )
    result = parse_orca_output_text(text, source_path="three_tables.out")
    assert result.n_atoms == expected_count
    assert result.coordinates == (
        [("O", 0.0, 0.0, 0.0), ("H", 1.0, 0.0, 0.0), ("H", 0.0, 1.0, 0.0)] if expected_count else []
    )
    assert result.energy_hartree == -77.0


@pytest.mark.parametrize("declared_counts", [["3"], ["3", "3"], ["3", "2"], ["BROKEN"], ["0"]])
def test_declared_atom_count_rejects_shortened_coordinate_tables(
    declared_counts: list[str],
) -> None:
    text = "".join(f"Number of atoms                         .... {n}\n" for n in declared_counts)
    text += (
        "CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
        "O 0.0 0.0 0.0\nH 1.0 0.0 0.0\n\nFINAL SINGLE POINT ENERGY -75.0\n"
    )
    result = parse_orca_output_text(text, source_path="declared_count.out")
    assert result.coordinates == []
    assert result.n_atoms == 0


@pytest.mark.parametrize("boundary", ["* O   R   C   A *", "****ORCA TERMINATED NORMALLY****"])
def test_coordinate_atom_count_does_not_cross_job_boundary(boundary: str) -> None:
    text = (
        "Number of atoms                         .... 3\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
        "O 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.0 1.0 0.0\n\n"
        "FINAL SINGLE POINT ENERGY -75.0\n"
        f"{boundary}\n"
        "Number of atoms                         .... 2\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
        "H 0.0 0.0 0.0\nH 0.0 0.0 0.74\n\n"
        "FINAL SINGLE POINT ENERGY -1.1\n"
    )
    result = parse_orca_output_text(text, source_path="concatenated.out")
    assert result.coordinates == [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)]
    assert result.energy_hartree == -1.1


@pytest.mark.parametrize("shorten_final", [False, True], ids=["complete_final", "short_final"])
def test_authentic_water_intervening_truncation_keeps_established_count(
    shorten_final: bool,
) -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "orca_6_1_1" / "water_opt_freq.out"
    header = "CARTESIAN COORDINATES (ANGSTROEM)"
    sections = read_orca_text(str(fixture)).split(header)
    for index in [-2, -1] if shorten_final else [-2]:
        lines = sections[index].splitlines(keepends=True)
        assert lines[4].lstrip().startswith("H ")
        del lines[4]
        sections[index] = "".join(lines)
    result = parse_orca_output_text(header.join(sections), source_path=str(fixture))
    assert result.energy_hartree == pytest.approx(-74.965901189921, abs=1e-10)
    assert result.coordinates == (
        []
        if shorten_final
        else [
            ("O", 0.066935, 0.0, 0.066935),
            ("H", -0.019526, 0.0, 1.052591),
            ("H", 1.052591, 0.0, -0.019526),
        ]
    )


@pytest.mark.parametrize("cases", [("water_opt_freq", "h2_sp"), ("h2_sp", "water_opt_freq")])
@pytest.mark.parametrize("shorten_final", [False, True])
def test_authentic_concatenation_does_not_share_coordinate_counts(
    cases: tuple[str, str], shorten_final: bool
) -> None:
    # Only coordinate-count isolation is asserted; this does not establish
    # support for a compound/multi-job result's other metadata.
    fixtures = Path(__file__).parents[2] / "fixtures" / "orca_6_1_1"
    first, last = [read_orca_text(str(fixtures / f"{case}.out")) for case in cases]
    expected = (
        [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)]
        if cases[1] == "h2_sp"
        else [
            ("O", 0.066935, 0.0, 0.066935),
            ("H", -0.019526, 0.0, 1.052591),
            ("H", 1.052591, 0.0, -0.019526),
        ]
    )
    if shorten_final:
        header = "CARTESIAN COORDINATES (ANGSTROEM)"
        prefix, section = last.rsplit(header, 1)
        lines = section.splitlines(keepends=True)
        del lines[2 + len(expected) - 1]
        last = prefix + header + "".join(lines)
    result = parse_orca_output_text(first + last, source_path="concatenated.out")
    assert result.coordinates == ([] if shorten_final else expected)
    assert len(expected) == (2 if cases[1] == "h2_sp" else 3)


@pytest.mark.parametrize("prefix", ["|  7> ", "# ", "diagnostic: "])
def test_echoed_atom_count_and_job_banner_do_not_override_geometry(prefix: str) -> None:
    text = (
        "Number of atoms                         .... 3\n"
        f"{prefix}* O   R   C   A *\n"
        f"{prefix}Number of atoms                         .... 2\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
        "O 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.0 1.0 0.0\n\n"
        "FINAL SINGLE POINT ENERGY -75.0\n"
    )
    result = parse_orca_output_text(text, source_path="echoed_count.out")
    assert result.n_atoms == 3
    assert result.coordinates == [("O", 0.0, 0.0, 0.0), ("H", 1.0, 0.0, 0.0), ("H", 0.0, 1.0, 0.0)]
