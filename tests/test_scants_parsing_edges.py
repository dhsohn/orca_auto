"""Edge-case fixtures for the ScanTS input/output parsing in orca/scants.py.

These tests pin the fail-closed behavior of the surface parser, the scan-line
rewriters, and the ``prepare_scants_*`` builders against malformed, truncated,
or already-exhausted inputs — the cases an ORCA version bump or a corrupt run
directory would surface first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.scants import (
    _continue_simple_scan_line,
    _format_scan_float,
    _remove_geom_scan_subblock,
    _replace_scants_route_with_optts,
    _resume_simple_scan_line,
    apply_scants_failed_scan_retry_rewrite,
    apply_scants_optts_resume_rewrite,
    apply_scants_relaxed_scan_resume_rewrite,
    output_indicates_scants_optts_refinement,
    parse_scants_actual_surface,
    prepare_scants_optts_fallback_input,
    prepare_scants_scan_retry_input,
    validate_scan_coordinate_lines,
)


def _scants_lines(
    *,
    route_lines: tuple[str, ...] = ("! ScanTS B3LYP def2-SVP D3BJ Freq",),
    directives: tuple[str, ...] = (),
    scan_lines: tuple[str, ...] | None = ("    B 4 20 = 1.86, 3.40, 32",),
    geometry: str | None = "* xyzfile 0 1 input.xyz",
) -> list[str]:
    lines = [*route_lines, "", *directives, "%geom", "  MaxIter 200"]
    if scan_lines is not None:
        lines.extend(["  Scan", *scan_lines, "  end"])
    lines.extend(["end", ""])
    if geometry is not None:
        lines.append(geometry)
    return lines


def _write_inp(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_scan_xyz_series(root: Path, stem: str, count: int) -> None:
    for idx in range(1, count + 1):
        (root / f"{stem}.{idx:03d}.xyz").write_text(
            f"2\nscan step {idx}\nH 0 0 0\nH 0 0 {idx}.0\n",
            encoding="utf-8",
        )


def test_parse_scants_actual_surface_skips_lines_without_two_floats(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "RELAXED SURFACE SCAN RESULTS",
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "",
                "   (interpolated point omitted)",
                "   1.91000000 -99.50000000",
                "The Calculated Surface using the SCF energy",
                "   1.86000000 -101.00000000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    points = parse_scants_actual_surface(out_path)

    assert [point.index for point in points] == [1, 2]
    assert points[1].energy == pytest.approx(-99.5)


def test_surface_and_refinement_parsers_tolerate_missing_output(tmp_path: Path) -> None:
    missing = tmp_path / "absent.out"
    assert parse_scants_actual_surface(missing) == []
    assert output_indicates_scants_optts_refinement(missing) is False


def test_strict_scan_coordinate_rejects_unclosed_geom_nesting() -> None:
    with pytest.raises(ValueError, match="closed"):
        validate_scan_coordinate_lines(
            ["%geom", "  Scan", "    B 0 1 = 1.2, 3.0, 10", "  end"],
            atom_count=2,
        )


def test_strict_scan_coordinate_rejects_duplicate_active_geom_blocks() -> None:
    lines = _scants_lines()
    lines[lines.index("* xyzfile 0 1 input.xyz") : lines.index("* xyzfile 0 1 input.xyz")] = [
        "%geom",
        "  MaxIter 50",
        "end",
    ]

    with pytest.raises(ValueError, match="exactly one active %geom"):
        validate_scan_coordinate_lines(lines, atom_count=32)


def test_relaxed_scan_resume_requires_progress_and_scan_block(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    lines = _scants_lines()
    _write_inp(source_inp, lines)

    # no numbered xyz yet: nothing to resume from
    assert apply_scants_relaxed_scan_resume_rewrite(list(lines), source_inp) == []

    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    # progress exists but the input carries no %geom block at all
    no_geom = ["! ScanTS B3LYP def2-SVP", "", "* xyzfile 0 1 input.xyz"]
    assert apply_scants_relaxed_scan_resume_rewrite(no_geom, source_inp) == []

    # two scan dimensions disagreeing on the shared point total
    mismatched = _scants_lines(
        scan_lines=(
            "    B 4 20 = 1.86, 3.40, 32",
            "    A 3 4 5 = 100.0, 120.0, 16",
        ),
    )
    assert apply_scants_relaxed_scan_resume_rewrite(mismatched, source_inp) == []


def test_relaxed_scan_resume_past_endpoint_fails_closed(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    lines = _scants_lines()
    _write_inp(source_inp, lines)
    _write_scan_xyz_series(tmp_path, "rxn", count=32)

    # all 32 points already produced: there is no remaining range to resume
    assert apply_scants_relaxed_scan_resume_rewrite(list(lines), source_inp) == []


def test_failed_scan_retry_rewrite_requires_retry_and_paths(tmp_path: Path) -> None:
    lines = _scants_lines()
    assert apply_scants_failed_scan_retry_rewrite(list(lines), retry_number=0) == []
    assert (
        apply_scants_failed_scan_retry_rewrite(
            list(lines),
            retry_number=1,
            source_inp=None,
            target_inp=tmp_path / "rxn.retry1.inp",
        )
        == []
    )


def test_failed_scan_retry_rewrite_tolerates_missing_route_line(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    lines = ["%geom", "  Scan", "    B 4 20 = 1.86, 3.40, 32", "  end", "end"]
    _write_inp(source_inp, lines)

    # checkpoint cleanup must not crash on a route-less fragment; without any
    # numbered xyz progress the recipe then fails closed
    assert (
        apply_scants_failed_scan_retry_rewrite(
            list(lines),
            retry_number=1,
            source_inp=source_inp,
            target_inp=tmp_path / "rxn.retry1.inp",
        )
        == []
    )


def test_scan_retry_builder_fails_closed_without_scan_block(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(scan_lines=None))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    assert prepare_scants_scan_retry_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.retry1.inp",
        retry_number=1,
    ) == (None, [])


def test_scan_retry_builder_fails_closed_when_progress_exceeds_extension(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines())
    # 32-point scan extends by max(6, 20%) = 6 steps; 38 completed points leave
    # no remaining range
    _write_scan_xyz_series(tmp_path, "rxn", count=38)

    assert prepare_scants_scan_retry_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.retry1.inp",
        retry_number=1,
    ) == (None, [])


def test_scan_retry_builder_fails_closed_for_zero_width_scan(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(scan_lines=("    B 4 20 = 2.00, 2.00, 32",)))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    # start == end means a zero step: the continuation cannot move the range
    assert prepare_scants_scan_retry_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.retry1.inp",
        retry_number=1,
    ) == (None, [])


def test_scan_retry_builder_requires_geometry_line(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(geometry=None))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    assert prepare_scants_scan_retry_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.retry1.inp",
        retry_number=1,
    ) == (None, [])


def test_optts_fallback_requires_existing_guess_xyz(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines())
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   1.91000000 -99.50000000",
                "   1.96000000 -99.75000000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # the surface names point 2 as the maximum but rxn.002.xyz was never written
    assert prepare_scants_optts_fallback_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.optts.inp",
        out_path=out_path,
    ) == (None, [])


def test_optts_fallback_requires_geometry_line(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(geometry=None))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   1.91000000 -99.50000000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert prepare_scants_optts_fallback_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.optts.inp",
        out_path=out_path,
    ) == (None, [])


def test_optts_resume_rewrite_requires_geometry_line(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    lines = _scants_lines(geometry=None)
    _write_inp(source_inp, lines)
    _write_scan_xyz_series(tmp_path, "rxn", count=2)
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   1.91000000 -99.50000000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        apply_scants_optts_resume_rewrite(
            lines=list(lines),
            source_inp=source_inp,
            target_inp=tmp_path / "rxn.resume.inp",
            out_path=out_path,
        )
        == []
    )


def test_scan_line_rewriters_reject_unparseable_lines() -> None:
    garbage = "  MaxIter 200"
    assert _resume_simple_scan_line(garbage, completed_points=3) is None
    assert (
        _continue_simple_scan_line(garbage, completed_points=3, extension_steps=6, new_points=5)
        is None
    )


def test_scan_line_rewriters_reject_impossible_progress() -> None:
    line = "    B 4 20 = 1.86, 3.40, 32"
    assert (
        _continue_simple_scan_line(line, completed_points=0, extension_steps=6, new_points=5)
        is None
    )


def test_format_scan_float_normalizes_negative_zero() -> None:
    assert _format_scan_float(-0.0) == "0"
    assert _format_scan_float(-1e-9) == "-1e-09"
    assert _format_scan_float(1.86) == "1.86"


def test_route_and_scan_block_helpers_tolerate_missing_targets() -> None:
    no_scants_route = ["! Opt B3LYP def2-SVP"]
    assert _replace_scants_route_with_optts(list(no_scants_route)) is False
    assert _remove_geom_scan_subblock(["! ScanTS", "* xyz 0 1", "H 0 0 0", "*"]) is False


def test_scants_mutators_use_active_text_after_closed_comments() -> None:
    lines = [
        "# route provenance # ! SCANTS Freq",
        "# block provenance # %geom",
        "# scan provenance # Scan",
        "# coordinate provenance # B 4 20 = 1.86, 3.40, 32",
        "# scan end provenance # end",
        "end",
        "* xyzfile 0 1 input.xyz",
    ]

    assert _replace_scants_route_with_optts(lines) is True
    assert lines[0] == "! OPTTS Freq"
    assert _remove_geom_scan_subblock(lines) is True
    assert not any("B 4 20" in line or "scan provenance" in line for line in lines)


def test_remove_geom_scan_subblock_handles_truncated_block() -> None:
    # a truncated input can end mid-scan with no closing `end` lines at all
    lines = ["! ScanTS B3LYP", "%geom", "  Scan", "    B 4 20 = 1.86, 3.40, 32"]
    assert _remove_geom_scan_subblock(lines) is True
    assert lines == ["! ScanTS B3LYP", "%geom"]


def test_prepare_builders_reject_non_scants_inputs(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    target_inp = tmp_path / "rxn.retry1.inp"
    _write_inp(source_inp, ["! Opt B3LYP def2-SVP", "", "* xyzfile 0 1 input.xyz"])
    out_path = tmp_path / "rxn.out"
    out_path.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    assert prepare_scants_scan_retry_input(
        source_inp=source_inp, target_inp=target_inp, retry_number=1
    ) == (None, [])
    assert prepare_scants_optts_fallback_input(
        source_inp=source_inp,
        target_inp=target_inp,
        out_path=out_path,
    ) == (None, [])


def test_prepared_input_clamps_maxcore_to_budget(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    target_inp = tmp_path / "rxn.retry1.inp"
    _write_inp(source_inp, _scants_lines(directives=("%maxcore 999999",)))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    prepared, actions = prepare_scants_scan_retry_input(
        source_inp=source_inp,
        target_inp=target_inp,
        retry_number=1,
        max_memory_gb=1,
    )

    assert prepared == target_inp
    assert "maxcore_clamped_to_budget" in actions
    assert "999999" not in target_inp.read_text(encoding="utf-8")
