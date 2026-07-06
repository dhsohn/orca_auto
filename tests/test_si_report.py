"""Per-job SI block (si_block.md) tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.orca.report.si import (
    collect_si_block,
    render_si_block_md,
    si_block_path,
    structure_kind,
    write_si_block,
)


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
    # through to the "sp" classification (Codex #48 P2).
    cases = (
        ("scan_job", _SCAN_INP),
        ("irc_job", _IRC_INP),
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

    path = write_si_block(reaction_dir, state)
    assert path is not None and path.exists()

    (reaction_dir / "job.inp").write_text(_SCAN_INP, encoding="utf-8")
    assert write_si_block(reaction_dir, state) is None
    assert not si_block_path(reaction_dir).exists()


def test_ts_block_parses_frequencies_from_utf16_output(tmp_path: Path) -> None:
    # ORCA can emit UTF-16 output; the frequency parser must decode it like the
    # main parser, or an opt+freq TS block loses Nimag and is misclassified
    # (Codex #48 P2).
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
