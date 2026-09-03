"""Per-job SI block (si_block.md) tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report.attempts import final_out_path
from orca_auto.orca.report.frequencies import parse_frequency_analysis
from orca_auto.orca.report.si import (
    SiBlockError,
    collect_si_block,
    parsed_final_output,
    render_si_block_md,
    si_block_path,
    structure_kind,
    write_si_block,
)
from tests.engine_artifact_helpers import report_generation_target


def _frequency_section(freqs: tuple[float, ...]) -> list[str]:
    return [
        "-----------------------",
        "VIBRATIONAL FREQUENCIES",
        "-----------------------",
        *(f"{index:4d}:   {freq:10.2f} cm**-1" for index, freq in enumerate(freqs)),
        "",
    ]


def test_frequency_block_before_the_final_energy_is_not_reported(tmp_path: Path) -> None:
    # OptTS with Calc_Hess and no Freq: the initial Hessian's block precedes
    # the optimization, so the final geometry has no frequency calculation.
    out = tmp_path / "optts_no_freq.out"
    out.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS",
                *_frequency_section((-650.0, 120.0)),
                "FINAL SINGLE POINT ENERGY      -100.100000000000",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    assert parse_frequency_analysis(out) is None


def test_frequency_block_after_the_last_final_energy_is_reported(tmp_path: Path) -> None:
    # OptTS Freq with Recalc_Hess: mid-optimization Hessian blocks are
    # superseded by later final energies; the final Freq block is kept.
    out = tmp_path / "optts_freq.out"
    out.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS Freq",
                *_frequency_section((-650.0, -120.0)),
                "------------",
                "NORMAL MODES",
                "------------",
                "                  0          1",
                "      0       0.100000   0.200000",
                "      1       0.300000   0.400000",
                "      2       0.500000   0.600000",
                "",
                "FINAL SINGLE POINT ENERGY      -100.100000000000",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  C      0.000000    0.000000    0.000000",
                "",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                *_frequency_section((-420.0, 120.0)),
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    analysis = parse_frequency_analysis(out)

    assert analysis is not None
    assert analysis.frequencies == (-420.0, 120.0)
    assert analysis.imaginary_count() == 1
    assert analysis.atoms == (("C", 0.0, 0.0, 0.0),)
    # The superseded Hessian's displacement vectors must not be paired with
    # the final frequencies.
    assert analysis.mode_matrix == {}


def _out_text(
    *,
    route: str = "wB97X-D3 def2-TZVP CPCM(toluene) OptTS Freq",
    energy: float = -1234.567890123456,
    freqs: tuple[float, ...] = (),
    thermo: bool = False,
) -> str:
    lines = [
        "                                 Program Version 6.0.1 -  RELEASE  -",
        f"|  1> ! {route}",
        "|  2> * xyz 0 1",
        "|  3> C 0.0 0.0 0.0",
        "|  4> *",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
        "  C      0.000000    1.234567   -0.987654",
        "  H      0.123456   -0.654321    2.000000",
        "",
        f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
        "THE OPTIMIZATION HAS CONVERGED",
    ]
    if freqs:
        lines += ["", "VIBRATIONAL FREQUENCIES", "-----------------------", ""]
        lines += [f"{index:6d}: {value:12.2f} cm**-1" for index, value in enumerate(freqs)]
        lines += [
            "",
            "NORMAL MODES",
            "------------",
            "",
            "                  0          1",
            "      0       0.700000   0.100000",
            "      1       0.100000   0.000000",
            "      2       0.000000   0.000000",
            "      3       0.500000   0.000000",
            "      4       0.000000   0.200000",
            "      5       0.000000   0.100000",
        ]
    if thermo:
        lines += [
            "--------------------------",
            "THERMOCHEMISTRY AT 298.15K",
            "--------------------------",
            "Zero point energy                ...      0.08843782 Eh",
            "Total enthalpy                   ...  -1234.40000000 Eh",
            "Final Gibbs free energy          ...  -1234.45000000 Eh",
            "G-E(el)                          ...      0.11789012 Eh",
        ]
    lines += [
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
    return "\n".join(lines)


def _job_dir(
    tmp_path: Path,
    name: str,
    *,
    inp_text: str,
    out_text: str,
) -> tuple[Path, dict[str, Any]]:
    reaction_dir = tmp_path / name
    reaction_dir.mkdir()
    inp = reaction_dir / "job.inp"
    inp.write_text(inp_text, encoding="utf-8")
    out = reaction_dir / "job.out"
    out.write_text(out_text, encoding="utf-8")
    state: dict[str, Any] = {
        "status": "completed",
        "selected_inp": str(inp),
        "attempts": [{"index": 1, "out_path": str(out)}],
        "final_result": {"last_out_path": str(out)},
    }
    return reaction_dir, state


_TS_INP = "! wB97X-D3 def2-TZVP CPCM(toluene) OptTS Freq\n* xyz 0 1\nC 0 0 0\n*\n"
_OPT_INP = "! B3LYP def2-SVP Opt Freq\n* xyz 0 1\nC 0 0 0\n*\n"
_SP_INP = "! wB97M-V def2-TZVPP\n* xyz 0 1\nC 0 0 0\n*\n"
_SCAN_INP = (
    "! B3LYP def2-SVP Opt\n"
    "%geom\n"
    "  Scan\n"
    "    B 0 1 = 1.0, 2.0, 5\n"
    "  end\n"
    "end\n"
    "* xyz 0 1\nC 0 0 0\n*\n"
)
_IRC_INP = "! B3LYP def2-SVP IRC\n* xyz 0 1\nC 0 0 0\n*\n"


def test_final_out_path_never_substitutes_an_earlier_attempt(tmp_path: Path) -> None:
    earlier = tmp_path / "attempt_1.out"
    earlier.write_text("earlier attempt\n", encoding="utf-8")
    missing_final = tmp_path / "attempt_2.out"

    # A recorded final output that is absent on disk must read as no output,
    # never as the previous attempt's file.
    assert (
        final_out_path(
            {
                "final_result": {"last_out_path": str(missing_final)},
                "attempts": [{"index": 1, "out_path": str(earlier)}],
            }
        )
        is None
    )
    # Records that never captured a final result path keep the attempt scan.
    assert final_out_path({"attempts": [{"index": 1, "out_path": str(earlier)}]}) == earlier


def test_ts_block_renders_thermochemistry_mode_and_coordinates(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "TS_candidate_03",
        inp_text=_TS_INP,
        out_text=_out_text(freqs=(-512.3, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    rendered = render_si_block_md(block)

    assert rendered.startswith("== TS_candidate_03 ==")
    assert "(ORCA 6.0.1)" in rendered
    assert "Charge 0, Multiplicity 1  (CH)" in rendered
    assert "E(el)" in rendered and "-1234.567890 Eh" in rendered
    assert "ZPE correction" in rendered
    assert "G-E(el)" in rendered
    assert "Nimag = 1" in rendered
    assert "ν‡ = -512.3 cm⁻¹" in rendered
    # 1-based atom numbering in the mode note
    assert "C1" in rendered
    assert "C       0.000000     1.234567    -0.987654" in rendered
    assert "⚠" not in rendered
    # The block names the output it was read from, before the coordinates.
    final_out = final_out_path(state)
    assert final_out is not None
    assert block.last_out_name == final_out.name
    assert rendered.index(f"Last output: {block.last_out_name}") < rendered.index(
        "C       0.000000"
    )


def test_minimum_with_imaginary_mode_gets_warning(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "opt_job",
        inp_text=_OPT_INP,
        out_text=_out_text(route="B3LYP def2-SVP Opt Freq", freqs=(-512.3, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.kind == "min"
    assert any("expected a minimum" in warning for warning in block.warnings)
    assert "⚠ expected a minimum but found 1 imaginary mode(s)" in render_si_block_md(block)


def test_uncharacterized_stationary_point_gets_warning(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "opt_no_freq",
        inp_text="! B3LYP def2-SVP Opt\n* xyz 0 1\nC 0 0 0\n*\n",
        out_text=_out_text(route="B3LYP def2-SVP Opt"),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert any("uncharacterized" in warning for warning in block.warnings)


def test_sp_block_has_no_nimag_and_no_warnings(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "sp_job",
        inp_text=_SP_INP,
        out_text=_out_text(route="wB97M-V def2-TZVPP"),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.kind == "sp"
    assert block.warnings == ()
    rendered = render_si_block_md(block)
    assert "Nimag" not in rendered


def test_non_stationary_jobs_get_no_block(tmp_path: Path) -> None:
    # Path/dynamics endpoints are not stationary points and must not fall
    # through to the "sp" classification.
    cases = (
        ("scan_job", _SCAN_INP),
        ("neb_job", "! NEB B3LYP def2-SVP\n* xyz 0 1\nC 0 0 0\n*\n"),
        ("neb_ci_job", "! ZOOM-NEB-CI B3LYP def2-SVP\n* xyz 0 1\nC 0 0 0\n*\n"),
        ("md_job", "! MD B3LYP def2-SVP\n* xyz 0 1\nC 0 0 0\n*\n"),
    )
    for name, inp_text in cases:
        reaction_dir, state = _job_dir(
            tmp_path, name, inp_text=inp_text, out_text=_out_text(route="B3LYP def2-SVP Opt")
        )
        assert structure_kind(Path(state["selected_inp"])) is None, name
        assert collect_si_block(reaction_dir, state) is None, name


def test_write_si_block_writes_irc_summary_without_coordinates(tmp_path: Path) -> None:
    irc_summary = """
----------------------
IRC PATH SUMMARY
----------------------
Step     E(Eh)        dE(kcal/mol)  max(|G|)  RMS(G)
 -1    -1234.590000   -13.88       0.00120   0.00050
  0    -1234.567890     0.00       0.00200   0.00090 <= TS
  1    -1234.585000   -10.74       0.00110   0.00045

"""
    reaction_dir, state = _job_dir(
        tmp_path,
        "irc_job",
        inp_text=_IRC_INP,
        out_text=_out_text(route="B3LYP def2-SVP IRC") + irc_summary,
    )

    assert structure_kind(Path(state["selected_inp"])) is None
    assert collect_si_block(reaction_dir, state) is None
    generation, identity = report_generation_target(reaction_dir)
    path = write_si_block(reaction_dir, state, generation_target=(generation, identity))

    assert path == generation / "si_block.md"
    rendered = path.read_text(encoding="utf-8")
    assert "IRC validation summary" in rendered
    assert "path endpoint 1" in rendered
    assert "TS step = 0" in rendered
    assert "optimize endpoints before publishing endpoint coordinates" in rendered
    assert "C       0.000000" not in rendered


def test_scan_functional_optimization_is_a_min_block(tmp_path: Path) -> None:
    # "SCAN" in a route line is the meta-GGA density functional, not a scan
    # job: an optimization with it must keep its SI block.
    reaction_dir, state = _job_dir(
        tmp_path,
        "scan_functional_job",
        inp_text="! SCAN def2-SVP Opt Freq\n* xyz 0 1\nC 0 0 0\n*\n",
        out_text=_out_text(route="SCAN def2-SVP Opt Freq", freqs=(30.0, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.kind == "min"
    assert block.warnings == ()


def test_neb_ts_route_is_still_a_ts_block(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "neb_ts_job",
        inp_text="! NEB-TS B3LYP def2-SVP Freq\n* xyz 0 1\nC 0 0 0\n*\n",
        out_text=_out_text(route="NEB-TS B3LYP def2-SVP Freq", freqs=(-512.3, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.kind == "ts"


def test_scants_route_is_a_ts_block(tmp_path: Path) -> None:
    inp_text = (
        "! ScanTS B3LYP def2-SVP Freq\n"
        "%geom\n  Scan\n    B 0 1 = 1.0, 2.0, 5\n  end\nend\n"
        "* xyz 0 1\nC 0 0 0\n*\n"
    )
    reaction_dir, state = _job_dir(
        tmp_path,
        "scants_job",
        inp_text=inp_text,
        out_text=_out_text(route="ScanTS B3LYP def2-SVP Freq", freqs=(-512.3, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.kind == "ts"


def test_incomplete_job_gets_no_block(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(tmp_path, "failed_job", inp_text=_TS_INP, out_text=_out_text())
    state["status"] = "failed"

    assert collect_si_block(reaction_dir, state) is None


def test_write_si_block_removes_stale_file_for_blockless_job(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path, "reused_dir", inp_text=_TS_INP, out_text=_out_text(freqs=(-512.3, 120.0))
    )

    generation, identity = report_generation_target(reaction_dir)
    target = (generation, identity)
    path = write_si_block(reaction_dir, state, generation_target=target)
    assert path is not None and path.exists()

    (reaction_dir / "job.inp").write_text(_SCAN_INP, encoding="utf-8")
    assert write_si_block(reaction_dir, state, generation_target=target) is None
    assert not si_block_path(generation).exists()


def test_ts_block_parses_frequencies_from_utf16_output(tmp_path: Path) -> None:
    # ORCA can emit UTF-16 output; the frequency parser must decode it like the
    # main parser, or an opt+freq TS block loses Nimag and is misclassified.
    reaction_dir = tmp_path / "utf16_ts"
    reaction_dir.mkdir()
    inp = reaction_dir / "job.inp"
    inp.write_text(_TS_INP, encoding="utf-8")
    out = reaction_dir / "job.out"
    out.write_text(_out_text(freqs=(-512.3, 120.0), thermo=True), encoding="utf-16")
    state: dict[str, Any] = {
        "status": "completed",
        "selected_inp": str(inp),
        "attempts": [{"index": 1, "out_path": str(out)}],
        "final_result": {"last_out_path": str(out)},
    }

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.imaginary_count == 1
    assert "Nimag = 1" in render_si_block_md(block)


def test_tightopt_route_is_a_min_block(tmp_path: Path) -> None:
    # TightOpt/COpt spellings are geometry optimizations; classifying them as
    # "sp" would silently drop the structure from the workflow SI energy table.
    reaction_dir, state = _job_dir(
        tmp_path,
        "tightopt_job",
        inp_text="! B3LYP def2-SVP TightOpt Freq\n* xyz 0 1\nC 0 0 0\n*\n",
        out_text=_out_text(route="B3LYP def2-SVP TightOpt Freq", freqs=(30.0, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.kind == "min"
    assert block.warnings == ()


def test_route_comment_does_not_change_structure_kind(tmp_path: Path) -> None:
    # A "# TS guess" note on the route line must not turn a minimum into a TS
    # block (the bare-TS / SCAN-functional collision class).
    inp = tmp_path / "job.inp"
    inp.write_text(
        "! B3LYP def2-SVP Opt Freq  # TS guess from scan\n* xyz 0 1\nC 0 0 0\n*\n",
        encoding="utf-8",
    )
    assert structure_kind(inp) == "min"


def test_unreadable_input_is_an_error_not_a_blockless_job(tmp_path: Path) -> None:
    # A vanished input (archived stage dir) must surface as an exclusion
    # reason, not masquerade as "job type has no SI block".
    reaction_dir, state = _job_dir(
        tmp_path,
        "archived_job",
        inp_text=_TS_INP,
        out_text=_out_text(freqs=(-512.3, 120.0), thermo=True),
    )
    Path(state["selected_inp"]).unlink()

    with pytest.raises(SiBlockError, match="route lines"):
        collect_si_block(reaction_dir, state)


def test_small_negative_modes_are_noise_not_imaginary(tmp_path: Path) -> None:
    # Same 10 cm^-1 cutoff as the completion analyzer: a -6 cm^-1 numerical
    # wobble on a verified TS must not publish Nimag = 2 against the
    # analyzer's own COMPLETED verdict.
    reaction_dir, state = _job_dir(
        tmp_path,
        "soft_mode_ts",
        inp_text=_TS_INP,
        out_text=_out_text(freqs=(-512.3, -6.2, 120.0), thermo=True),
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    assert block.imaginary_count == 1
    assert block.warnings == ()
    assert "Nimag = 1" in render_si_block_md(block)


def test_thermo_rows_omit_temperature_the_output_never_stated(tmp_path: Path) -> None:
    # No THERMOCHEMISTRY AT line parsed -> no fabricated "(298.15 K)" label;
    # the job may have run at a different %freq Temp.
    out_text = _out_text(freqs=(-512.3, 120.0), thermo=True).replace(
        "THERMOCHEMISTRY AT 298.15K", ""
    )
    reaction_dir, state = _job_dir(
        tmp_path, "unknown_temp_job", inp_text=_TS_INP, out_text=out_text
    )

    block = collect_si_block(reaction_dir, state)
    assert block is not None
    rendered = render_si_block_md(block)
    assert "298.15" not in rendered
    assert any(line.startswith("G ") for line in rendered.splitlines())


def test_parsed_final_output_caches_by_mtime(tmp_path: Path) -> None:
    out = tmp_path / "job.out"
    out.write_text(_out_text(energy=-1.0), encoding="utf-8")
    os.utime(out, ns=(1_000_000_000, 1_000_000_000))

    first, _ = parsed_final_output(out)
    again, _ = parsed_final_output(out)
    assert again is first  # unchanged file -> cache hit, no re-parse

    out.write_text(_out_text(energy=-2.0), encoding="utf-8")
    os.utime(out, ns=(2_000_000_000, 2_000_000_000))
    second, _ = parsed_final_output(out)
    assert first.energy_hartree == pytest.approx(-1.0)
    assert second.energy_hartree == pytest.approx(-2.0)
