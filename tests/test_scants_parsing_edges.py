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
    _cumulative_numbered_xyz_index_from_geometry,
    _format_scan_float,
    _remove_geom_scan_subblock,
    _replace_scants_route_with_endpoint_opt,
    _replace_scants_route_with_optts,
    _restore_selected_scants_route,
    _resume_simple_scan_line,
    _reverse_continuation_scan_line,
    _reverse_simple_scan_line,
    apply_scants_failed_scan_retry_rewrite,
    apply_scants_optts_resume_rewrite,
    apply_scants_relaxed_scan_resume_rewrite,
    output_indicates_scants_optts_refinement,
    parse_scants_actual_surface,
    prepare_scants_endpoint_scan_input,
    prepare_scants_optts_fallback_input,
    prepare_scants_reverse_scan_retry_input,
    prepare_scants_scan_retry_input,
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


def test_endpoint_scan_builder_fail_closed_paths(tmp_path: Path) -> None:
    cases = {
        # ScanTS route but no scan sub-block: nothing to complete
        "no_scan_block": (_scants_lines(scan_lines=None), 3),
        # scan sub-block but no numbered xyz progress
        "no_progress": (_scants_lines(), 0),
        # every point already produced: no remaining range
        "past_endpoint": (_scants_lines(), 32),
        # scan and progress fine, but no geometry line to rewrite
        "no_geometry": (_scants_lines(geometry=None), 3),
    }
    for name, (lines, xyz_count) in cases.items():
        case_dir = tmp_path / name
        case_dir.mkdir()
        source_inp = case_dir / "rxn.inp"
        _write_inp(source_inp, lines)
        if xyz_count:
            _write_scan_xyz_series(case_dir, "rxn", count=xyz_count)

        assert prepare_scants_endpoint_scan_input(
            source_inp=source_inp,
            target_inp=case_dir / "rxn.endpoint.inp",
        ) == (None, []), name


def test_endpoint_scan_builder_fails_closed_when_scants_is_not_a_route_token(
    tmp_path: Path,
) -> None:
    """The regex predicate sees ScanTS inside a decorated token; the tokenized
    route rewriter does not. The builder must fail closed on the divergence."""
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(route_lines=("! B3LYP ScanTS(broken) Freq",)))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    assert prepare_scants_endpoint_scan_input(
        source_inp=source_inp,
        target_inp=tmp_path / "rxn.endpoint.inp",
    ) == (None, [])


def test_endpoint_scan_route_keeps_existing_opt_before_scants(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    target_inp = tmp_path / "rxn.endpoint.inp"
    _write_inp(source_inp, _scants_lines(route_lines=("! Opt ScanTS B3LYP def2-SVP Freq",)))
    _write_scan_xyz_series(tmp_path, "rxn", count=3)

    prepared, actions = prepare_scants_endpoint_scan_input(
        source_inp=source_inp,
        target_inp=target_inp,
    )

    assert prepared == target_inp
    assert "scants_endpoint_scan_route_to_opt" in actions
    assert "scants_endpoint_scan_removed_freq_irc" in actions
    route = target_inp.read_text(encoding="utf-8").splitlines()[0]
    assert route == "! Opt B3LYP def2-SVP"


def test_reverse_scan_builder_requires_scan_block_and_progress(tmp_path: Path) -> None:
    no_block_dir = tmp_path / "no_block"
    no_block_dir.mkdir()
    no_block_inp = no_block_dir / "rxn.inp"
    _write_inp(no_block_inp, _scants_lines(scan_lines=None))
    assert prepare_scants_reverse_scan_retry_input(
        source_inp=no_block_inp,
        selected_inp=no_block_inp,
        target_inp=no_block_dir / "rxn.reverse.inp",
    ) == (None, [])

    no_xyz_dir = tmp_path / "no_xyz"
    no_xyz_dir.mkdir()
    no_xyz_inp = no_xyz_dir / "rxn.inp"
    _write_inp(no_xyz_inp, _scants_lines())
    assert prepare_scants_reverse_scan_retry_input(
        source_inp=no_xyz_inp,
        selected_inp=no_xyz_inp,
        target_inp=no_xyz_dir / "rxn.reverse.inp",
    ) == (None, [])


def test_reverse_scan_builder_fails_closed_for_zero_width_scan(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(scan_lines=("    B 4 20 = 2.00, 2.00, 32",)))
    _write_scan_xyz_series(tmp_path, "rxn", count=32)

    # reversing a zero-width range rewrites every scan line to itself
    assert prepare_scants_reverse_scan_retry_input(
        source_inp=source_inp,
        selected_inp=source_inp,
        target_inp=tmp_path / "rxn.reverse.inp",
    ) == (None, [])


def test_reverse_scan_builder_requires_geometry_line(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    _write_inp(source_inp, _scants_lines(geometry=None))
    _write_scan_xyz_series(tmp_path, "rxn", count=32)

    assert prepare_scants_reverse_scan_retry_input(
        source_inp=source_inp,
        selected_inp=source_inp,
        target_inp=tmp_path / "rxn.reverse.inp",
    ) == (None, [])


def test_reverse_scan_continuation_requires_matching_selected_scan(tmp_path: Path) -> None:
    # selected input lost its scan sub-block: the continuation cannot pair lines
    no_block_dir = tmp_path / "selected_without_scan"
    no_block_dir.mkdir()
    selected_inp = no_block_dir / "foo.inp"
    _write_inp(selected_inp, _scants_lines(scan_lines=None))
    source_inp = no_block_dir / "cont.inp"
    _write_inp(
        source_inp,
        _scants_lines(
            scan_lines=("    B 4 20 = 3.40, 4.10, 6",),
            geometry="* xyzfile 0 1 foo.005.xyz",
        ),
    )
    _write_scan_xyz_series(no_block_dir, "cont", count=6)
    assert prepare_scants_reverse_scan_retry_input(
        source_inp=source_inp,
        selected_inp=selected_inp,
        target_inp=no_block_dir / "cont.reverse.inp",
    ) == (None, [])

    # selected input scans two coordinates while the continuation scans one
    mismatch_dir = tmp_path / "selected_dimension_mismatch"
    mismatch_dir.mkdir()
    selected_inp = mismatch_dir / "foo.inp"
    _write_inp(
        selected_inp,
        _scants_lines(
            scan_lines=(
                "    B 4 20 = 1.86, 3.40, 32",
                "    B 1 2 = 1.00, 2.00, 32",
            ),
        ),
    )
    source_inp = mismatch_dir / "cont.inp"
    _write_inp(
        source_inp,
        _scants_lines(
            scan_lines=("    B 4 20 = 3.40, 4.10, 6",),
            geometry="* xyzfile 0 1 foo.005.xyz",
        ),
    )
    _write_scan_xyz_series(mismatch_dir, "cont", count=6)
    assert prepare_scants_reverse_scan_retry_input(
        source_inp=source_inp,
        selected_inp=selected_inp,
        target_inp=mismatch_dir / "cont.reverse.inp",
    ) == (None, [])


def test_reverse_scan_fails_closed_when_selected_route_cannot_be_restored(
    tmp_path: Path,
) -> None:
    # the selected input keeps ScanTS on its second route line; the endpoint
    # continuation collapsed to a single route line, so there is no matching
    # ordinal to restore
    selected_inp = tmp_path / "foo.inp"
    _write_inp(
        selected_inp,
        _scants_lines(route_lines=("! B3LYP def2-SVP", "! ScanTS TightSCF")),
    )
    source_inp = tmp_path / "cont.inp"
    _write_inp(source_inp, _scants_lines(route_lines=("! Opt B3LYP def2-SVP",)))
    _write_scan_xyz_series(tmp_path, "cont", count=32)

    assert prepare_scants_reverse_scan_retry_input(
        source_inp=source_inp,
        selected_inp=selected_inp,
        target_inp=tmp_path / "cont.reverse.inp",
    ) == (None, [])


def test_restore_selected_scants_route_edge_cases(tmp_path: Path) -> None:
    selected_without_scants = tmp_path / "plain.inp"
    _write_inp(selected_without_scants, ["! Opt B3LYP def2-SVP"])
    assert _restore_selected_scants_route(["! Opt B3LYP def2-SVP"], selected_without_scants) == []

    # the route already matches the selected input: nothing to restore
    selected_inp = tmp_path / "foo.inp"
    _write_inp(selected_inp, ["! ScanTS B3LYP def2-SVP"])
    assert _restore_selected_scants_route(["! ScanTS B3LYP def2-SVP"], selected_inp) == []


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
        reaction_dir=tmp_path,
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
        reaction_dir=tmp_path,
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
    assert _reverse_simple_scan_line(garbage) is None
    assert _reverse_continuation_scan_line(garbage, garbage, completed_points=3) is None
    assert (
        _continue_simple_scan_line(garbage, completed_points=3, extension_steps=6, new_points=5)
        is None
    )


def test_scan_line_rewriters_reject_impossible_progress() -> None:
    line = "    B 4 20 = 1.86, 3.40, 32"
    assert _reverse_continuation_scan_line(line, line, completed_points=0) is None
    assert (
        _continue_simple_scan_line(line, completed_points=0, extension_steps=6, new_points=5)
        is None
    )


def test_format_scan_float_normalizes_negative_zero() -> None:
    assert _format_scan_float(-1e-9) == "0"
    assert _format_scan_float(1.86) == "1.86"


def test_route_and_scan_block_helpers_tolerate_missing_targets() -> None:
    no_scants_route = ["! Opt B3LYP def2-SVP"]
    assert _replace_scants_route_with_endpoint_opt(list(no_scants_route)) == []
    assert _replace_scants_route_with_optts(list(no_scants_route)) is False
    assert _remove_geom_scan_subblock(["! ScanTS", "* xyz 0 1", "H 0 0 0", "*"]) is False


def test_remove_geom_scan_subblock_handles_truncated_block() -> None:
    # a truncated input can end mid-scan with no closing `end` lines at all
    lines = ["! ScanTS B3LYP", "%geom", "  Scan", "    B 4 20 = 1.86, 3.40, 32"]
    assert _remove_geom_scan_subblock(lines) is True
    assert lines == ["! ScanTS B3LYP", "%geom"]


def test_cumulative_scan_index_edge_cases(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"

    # internal-coordinate geometry carries no xyzfile reference
    internal = ["! ScanTS B3LYP", "* xyz 0 1", "H 0 0 0", "*"]
    assert _cumulative_numbered_xyz_index_from_geometry(internal, source_inp=source_inp) is None

    # a geometry referencing the source's own numbered xyz must not recurse
    self_ref = ["* xyzfile 0 1 rxn.003.xyz"]
    _write_inp(source_inp, self_ref)
    assert _cumulative_numbered_xyz_index_from_geometry(self_ref, source_inp=source_inp) == 3

    # missing parent input falls back to the local index
    missing_parent = ["* xyzfile 0 1 other.005.xyz"]
    assert _cumulative_numbered_xyz_index_from_geometry(missing_parent, source_inp=source_inp) == 5

    # unreadable parent input falls back to the local index
    (tmp_path / "weird.inp").mkdir()
    unreadable_parent = ["* xyzfile 0 1 weird.004.xyz"]
    assert (
        _cumulative_numbered_xyz_index_from_geometry(unreadable_parent, source_inp=source_inp) == 4
    )


def test_cumulative_scan_index_stops_on_reference_cycles(tmp_path: Path) -> None:
    a_inp = tmp_path / "a.inp"
    b_inp = tmp_path / "b.inp"
    _write_inp(a_inp, ["* xyzfile 0 1 b.002.xyz"])
    _write_inp(b_inp, ["* xyzfile 0 1 a.003.xyz"])

    # a -> b(.002) -> a(.003): the revisit of a stops the walk, so the chain
    # sums b's local index with a's contribution exactly once
    assert (
        _cumulative_numbered_xyz_index_from_geometry(["* xyzfile 0 1 b.002.xyz"], source_inp=a_inp)
        == 5
    )


def test_prepare_builders_reject_non_scants_inputs(tmp_path: Path) -> None:
    source_inp = tmp_path / "rxn.inp"
    target_inp = tmp_path / "rxn.retry1.inp"
    _write_inp(source_inp, ["! Opt B3LYP def2-SVP", "", "* xyzfile 0 1 input.xyz"])
    out_path = tmp_path / "rxn.out"
    out_path.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    assert prepare_scants_endpoint_scan_input(source_inp=source_inp, target_inp=target_inp) == (
        None,
        [],
    )
    assert prepare_scants_scan_retry_input(
        source_inp=source_inp, target_inp=target_inp, retry_number=1
    ) == (None, [])
    assert prepare_scants_reverse_scan_retry_input(
        source_inp=source_inp, selected_inp=source_inp, target_inp=target_inp
    ) == (None, [])
    assert prepare_scants_optts_fallback_input(
        source_inp=source_inp,
        target_inp=target_inp,
        reaction_dir=tmp_path,
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
