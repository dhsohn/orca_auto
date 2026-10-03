"""Per-job SI block (si_block.md) tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.completion_rules import route_facts
from orca_auto.orca.evidence import (
    OrcaEvidenceError,
    OrcaStructureEvidence,
    collect_structure_evidence,
    final_out_path,
    parsed_final_output,
    parsed_frequency_analysis,
    structure_kind,
)
from orca_auto.orca.report.si import (
    render_si_block_md,
    si_block_path,
    write_si_block,
)
from tests.engine_artifact_helpers import report_generation_target
from tests.orca_output_helpers import frequency_section, si_out_text


def test_frequency_block_before_the_final_energy_is_not_reported(tmp_path: Path) -> None:
    # OptTS with Calc_Hess and no Freq: the initial Hessian's block precedes
    # the optimization, so the final geometry has no frequency calculation.
    out = tmp_path / "optts_no_freq.out"
    out.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS",
                *frequency_section((-650.0, 120.0)),
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

    assert parsed_frequency_analysis(out) is None


def test_frequency_block_after_the_last_final_energy_is_reported(tmp_path: Path) -> None:
    # OptTS Freq with Recalc_Hess: mid-optimization Hessian blocks are
    # superseded by later final energies; the final Freq block is kept.
    out = tmp_path / "optts_freq.out"
    out.write_text(
        "\n".join(
            [
                "|  1> ! B3LYP def2-SVP OptTS Freq",
                *frequency_section((-650.0, -120.0)),
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
                *frequency_section((-420.0, 120.0)),
                "                             ****ORCA TERMINATED NORMALLY****",
            ]
        ),
        encoding="utf-8",
    )

    analysis = parsed_frequency_analysis(out)

    assert analysis is not None
    assert analysis.frequencies == (-420.0, 120.0)
    assert analysis.imaginary_count() == 1
    assert analysis.atoms == (("C", 0.0, 0.0, 0.0),)
    # The superseded Hessian's displacement vectors must not be paired with
    # the final frequencies.
    assert analysis.mode_matrix == {}


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


def _evidence(reaction_dir: Path, state: dict[str, Any]) -> OrcaStructureEvidence | None:
    return collect_structure_evidence(reaction_dir, state, route_facts(Path(state["selected_inp"])))


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


@pytest.mark.parametrize("geometry_line", ["", "| 2> # previous input: * xyz 0 1"])
def test_si_block_does_not_publish_unverified_electronic_state(
    tmp_path: Path, geometry_line: str
) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "missing_electronic_state",
        inp_text=_SP_INP,
        out_text=si_out_text().replace("|  2> * xyz 0 1", geometry_line),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    rendered = render_si_block_md(block)

    assert "Charge 0, Multiplicity 1" not in rendered
    assert "Charge / multiplicity: unavailable" in rendered
    assert "E(el)" in rendered


def test_si_block_publishes_verified_uppercase_geometry_state(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "uppercase_electronic_state",
        inp_text=_SP_INP.replace("* xyz 0 1", "* XYZ -1 2"),
        out_text=si_out_text().replace("* xyz 0 1", "* XYZ -1 2"),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert "Charge -1, Multiplicity 2" in render_si_block_md(block)


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


def test_missing_output_route_does_not_invent_a_single_point_label(tmp_path: Path) -> None:
    output = "\n".join(
        line for line in si_out_text().splitlines() if not line.startswith("|  1> !")
    )
    reaction_dir, state = _job_dir(
        tmp_path,
        "missing_route",
        inp_text=_TS_INP,
        out_text=output,
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    rendered = render_si_block_md(block)

    assert rendered.splitlines()[1] == "!         (ORCA 6.0.1)"
    assert "E(el)" in rendered and "-1234.567890 Eh" in rendered
    assert "Charge 0, Multiplicity 1  (CH)" in rendered


def test_ts_block_renders_thermochemistry_mode_and_coordinates(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "TS_candidate_03",
        inp_text=_TS_INP,
        out_text=si_out_text(freqs=(-512.3, 120.0), thermo=True),
    )

    block = _evidence(reaction_dir, state)
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
        out_text=si_out_text(route="B3LYP def2-SVP Opt Freq", freqs=(-512.3, 120.0), thermo=True),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert block.kind == "min"
    assert "expected a minimum" in render_si_block_md(block)
    assert "⚠ expected a minimum but found 1 imaginary mode(s)" in render_si_block_md(block)


def test_uncharacterized_stationary_point_gets_warning(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "opt_no_freq",
        inp_text="! B3LYP def2-SVP Opt\n* xyz 0 1\nC 0 0 0\n*\n",
        out_text=si_out_text(route="B3LYP def2-SVP Opt"),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert "uncharacterized" in render_si_block_md(block)


def test_sp_block_has_no_nimag_and_no_warnings(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "sp_job",
        inp_text=_SP_INP,
        out_text=si_out_text(route="wB97M-V def2-TZVPP"),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert block.kind == "sp"
    assert "⚠" not in render_si_block_md(block)
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
            tmp_path, name, inp_text=inp_text, out_text=si_out_text(route="B3LYP def2-SVP Opt")
        )
        assert structure_kind(route_facts(Path(state["selected_inp"]))) is None, name
        assert _evidence(reaction_dir, state) is None, name


@pytest.mark.parametrize(
    "geom_block",
    [
        "%geom Scan\n  B 0 1 = 1.0, 2.0, 5\n  end\nend\n",
        "%geom\n  MaxIter 80\n  Scan D 0 1 2 3 = 84.96, -92.24, 19 end\nend\n",
        "%geom\n  Scan\n    B 0 1 [1.0 1.1 1.2]\n  end\nend\n",
        # A scan block whose coordinate cannot be read is still a scan.
        "%geom\n  Scan\n    B 0 1 = 1.0 to 2.0\n  end\nend\n",
        "%geom\n  Constraints {B 1 2 C} end\n  Scan\n    B 0 1 = 1.0, 2.0, 5\n  end\nend\n",
        "%geom MaxIter 100 end\n%geom Scan\n  B 0 1 = 1.0, 2.0, 5\n  end\nend\n",
        "%geom\n  modify_internal\n  { B 1 2 A }\n  end\n  Scan\n    B 0 1 = 1.0, 2.0, 5\n"
        "  end\nend\n",
        "%geom\n  Hybrid_Hess {0 1} end\n  Scan\n    B 0 1 = 1.0, 2.0, 5\n  end\nend\n",
    ],
    ids=[
        "header",
        "single-line",
        "value-list",
        "unreadable",
        "constraints-then-scan",
        "second-geom-block",
        "modify-internal-then-scan",
        "hybrid-hess-then-scan",
    ],
)
def test_every_relaxed_scan_form_gets_no_block(tmp_path: Path, geom_block: str) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "scan_job",
        inp_text="! B3LYP def2-SVP Opt\n" + geom_block + "* xyz 0 1\nC 0 0 0\n*\n",
        out_text=si_out_text(route="B3LYP def2-SVP Opt"),
    )

    assert structure_kind(route_facts(Path(state["selected_inp"]))) is None
    assert _evidence(reaction_dir, state) is None


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
        out_text=si_out_text(route="B3LYP def2-SVP IRC") + irc_summary,
    )

    assert structure_kind(route_facts(Path(state["selected_inp"]))) is None
    assert _evidence(reaction_dir, state) is None
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
        out_text=si_out_text(route="SCAN def2-SVP Opt Freq", freqs=(30.0, 120.0), thermo=True),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert block.kind == "min"
    assert "⚠" not in render_si_block_md(block)


def test_neb_ts_route_is_still_a_ts_block(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path,
        "neb_ts_job",
        inp_text="! NEB-TS B3LYP def2-SVP Freq\n* xyz 0 1\nC 0 0 0\n*\n",
        out_text=si_out_text(
            route="NEB-TS B3LYP def2-SVP Freq", freqs=(-512.3, 120.0), thermo=True
        ),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert block.kind == "ts"


def test_incomplete_job_gets_no_block(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(tmp_path, "failed_job", inp_text=_TS_INP, out_text=si_out_text())
    state["status"] = "failed"

    assert _evidence(reaction_dir, state) is None


def test_write_si_block_removes_stale_file_for_blockless_job(tmp_path: Path) -> None:
    reaction_dir, state = _job_dir(
        tmp_path, "reused_dir", inp_text=_TS_INP, out_text=si_out_text(freqs=(-512.3, 120.0))
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
    out.write_text(si_out_text(freqs=(-512.3, 120.0), thermo=True), encoding="utf-16")
    state: dict[str, Any] = {
        "status": "completed",
        "selected_inp": str(inp),
        "attempts": [{"index": 1, "out_path": str(out)}],
        "final_result": {"last_out_path": str(out)},
    }

    block = _evidence(reaction_dir, state)
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
        out_text=si_out_text(
            route="B3LYP def2-SVP TightOpt Freq", freqs=(30.0, 120.0), thermo=True
        ),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert block.kind == "min"
    assert "⚠" not in render_si_block_md(block)


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        # Looser convergence criteria still end on a stationary minimum.
        ("! B3LYP def2-SVP SloppyOpt Freq", "min"),
        ("! B3LYP def2-SVP CrudeOpt Freq", "min"),
        # Hydrogen-only optimizations leave the heavy atoms where they were.
        ("! B3LYP def2-SVP OptH Freq", "sp"),
        ("! B3LYP def2-SVP L-OptH", "sp"),
        ("! B3LYP def2-SVP TightOpt OptH", "sp"),
        ("! B3LYP def2-SVP OptTS OptH", "ts"),
        # QM/MM active-region and crossing-seam optima are not minima of the
        # full surface; ORCA aliases of one search classify alike.
        ("! XTB QMMMOpt", "sp"),
        ("! B3LYP def2-SVP SurfCrossOpt", "sp"),
        ("! B3LYP def2-SVP MECP-Opt", "sp"),
        ("! B3LYP def2-SVP CI-Opt", "sp"),
        ("! B3LYP def2-SVP ConicalIntersect-Opt", "sp"),
    ],
)
def test_optimization_keyword_structure_kind(tmp_path: Path, route: str, expected: str) -> None:
    inp = tmp_path / "job.inp"
    inp.write_text(f"{route}\n* xyz 0 1\nC 0 0 0\n*\n", encoding="utf-8")
    assert structure_kind(route_facts(inp)) == expected


@pytest.mark.parametrize(
    "route",
    [
        "! B3LYP def2-SVP SloppyOpt",
        "! B3LYP def2-SVP OptH",
        "! B3LYP def2-SVP Opt OptH",
        "! B3LYP def2-SVP TightOpt OptH",
        "! B3LYP def2-SVP L-OptH",
    ],
)
def test_relaxed_scan_of_any_optimization_gets_no_block(tmp_path: Path, route: str) -> None:
    inp_text = _SCAN_INP.replace("! B3LYP def2-SVP Opt\n", f"{route}\n")
    assert inp_text.startswith(f"{route}\n%geom")
    reaction_dir, state = _job_dir(
        tmp_path, "scan_job", inp_text=inp_text, out_text=si_out_text(route=route[2:])
    )

    assert structure_kind(route_facts(Path(state["selected_inp"]))) is None
    assert _evidence(reaction_dir, state) is None


def test_route_comment_does_not_change_structure_kind(tmp_path: Path) -> None:
    # A "# TS guess" note on the route line must not turn a minimum into a TS
    # block (the bare-TS / SCAN-functional collision class).
    inp = tmp_path / "job.inp"
    inp.write_text(
        "! B3LYP def2-SVP Opt Freq  # TS guess from scan\n* xyz 0 1\nC 0 0 0\n*\n",
        encoding="utf-8",
    )
    assert structure_kind(route_facts(inp)) == "min"


def test_unreadable_input_is_an_error_not_a_blockless_job(tmp_path: Path) -> None:
    # A vanished input (archived stage dir) must surface as an exclusion
    # reason, not masquerade as "job type has no SI block".
    reaction_dir, state = _job_dir(
        tmp_path,
        "archived_job",
        inp_text=_TS_INP,
        out_text=si_out_text(freqs=(-512.3, 120.0), thermo=True),
    )
    Path(state["selected_inp"]).unlink()

    with pytest.raises(OrcaEvidenceError, match="route lines"):
        _evidence(reaction_dir, state)


def test_small_negative_modes_are_noise_not_imaginary(tmp_path: Path) -> None:
    # Same 10 cm^-1 cutoff as the completion analyzer: a -6 cm^-1 numerical
    # wobble on a verified TS must not publish Nimag = 2 against the
    # analyzer's own COMPLETED verdict.
    reaction_dir, state = _job_dir(
        tmp_path,
        "soft_mode_ts",
        inp_text=_TS_INP,
        out_text=si_out_text(freqs=(-512.3, -6.2, 120.0), thermo=True),
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    assert block.imaginary_count == 1
    assert "⚠" not in render_si_block_md(block)
    assert "Nimag = 1" in render_si_block_md(block)


def test_thermo_rows_omit_temperature_the_output_never_stated(tmp_path: Path) -> None:
    # No THERMOCHEMISTRY AT line parsed -> no fabricated "(298.15 K)" label;
    # the job may have run at a different %freq Temp.
    out_text = si_out_text(freqs=(-512.3, 120.0), thermo=True).replace(
        "THERMOCHEMISTRY AT 298.15K", ""
    )
    reaction_dir, state = _job_dir(
        tmp_path, "unknown_temp_job", inp_text=_TS_INP, out_text=out_text
    )

    block = _evidence(reaction_dir, state)
    assert block is not None
    rendered = render_si_block_md(block)
    assert "298.15" not in rendered
    assert any(line.startswith("G ") for line in rendered.splitlines())


def test_parsed_final_output_caches_by_mtime(tmp_path: Path) -> None:
    out = tmp_path / "job.out"
    out.write_text(si_out_text(energy=-1.0), encoding="utf-8")
    os.utime(out, ns=(1_000_000_000, 1_000_000_000))

    first, _ = parsed_final_output(out)
    again, _ = parsed_final_output(out)
    assert again is first  # unchanged file -> cache hit, no re-parse

    out.write_text(si_out_text(energy=-2.0), encoding="utf-8")
    os.utime(out, ns=(2_000_000_000, 2_000_000_000))
    second, _ = parsed_final_output(out)
    assert first.energy_hartree == pytest.approx(-1.0)
    assert second.energy_hartree == pytest.approx(-2.0)


def test_si_block_fails_closed_on_stale_or_partial_final_coordinates(tmp_path: Path) -> None:
    out_text = "\n".join(
        [
            "|  1> ! wB97M-V def2-TZVPP",
            "|  2> * xyz 0 1",
            "|  3> C 0.0 0.0 0.0",
            "|  4> *",
            "CARTESIAN COORDINATES (ANGSTROEM)",
            "---------------------------------",
            "  C      0.000000    0.000000    0.000000",
            "  H      1.000000    0.000000    0.000000",
            "FINAL SINGLE POINT ENERGY      -100.100000",
            "CARTESIAN COORDINATES (ANGSTROEM)",
            "---------------------------------",
            "  C      0.500000    0.000000    0.000000",
            "  H      BROKEN",
            "",
            "FINAL SINGLE POINT ENERGY      -100.200000",
            "                             ****ORCA TERMINATED NORMALLY****",
        ]
    )
    reaction_dir, state = _job_dir(tmp_path, "stale_coords", inp_text=_SP_INP, out_text=out_text)

    with pytest.raises(OrcaEvidenceError, match="lacks a final energy or geometry"):
        _evidence(reaction_dir, state)

    generation, identity = report_generation_target(reaction_dir)
    assert write_si_block(reaction_dir, state, generation_target=(generation, identity)) is None
    assert not si_block_path(generation).exists()


def test_si_block_fails_closed_on_row_boundary_truncated_final_coordinates(tmp_path: Path) -> None:
    out_text = "\n".join(
        [
            "|  1> ! wB97M-V def2-TZVPP",
            "|  2> * xyz 0 1",
            "|  3> C 0.0 0.0 0.0",
            "|  4> *",
            "CARTESIAN COORDINATES (ANGSTROEM)",
            "---------------------------------",
            "  C      0.000000    0.000000    0.000000",
            "  H      1.000000    0.000000    0.000000",
            "FINAL SINGLE POINT ENERGY      -100.100000",
            "CARTESIAN COORDINATES (ANGSTROEM)",
            "---------------------------------",
            "  C      0.500000    0.000000    0.000000",
            "",
            "FINAL SINGLE POINT ENERGY      -100.200000",
            "                             ****ORCA TERMINATED NORMALLY****",
        ]
    )
    reaction_dir, state = _job_dir(
        tmp_path, "row_boundary_coords", inp_text=_SP_INP, out_text=out_text
    )

    with pytest.raises(OrcaEvidenceError, match="lacks a final energy or geometry"):
        _evidence(reaction_dir, state)

    generation, identity = report_generation_target(reaction_dir)
    assert write_si_block(reaction_dir, state, generation_target=(generation, identity)) is None
    assert not si_block_path(generation).exists()
