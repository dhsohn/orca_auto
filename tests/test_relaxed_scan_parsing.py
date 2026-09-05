"""Fail-closed relaxed-scan surface and coordinate parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.orca.relaxed_scan import (
    _format_scan_float,
    parse_scan_actual_surface,
    scan_profile_interior_barrier_kcal,
    validate_scan_coordinate_lines,
)


def _scan_lines(
    *,
    route_lines: tuple[str, ...] = ("! Opt B3LYP def2-SVP D3BJ Freq",),
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


def test_parse_scan_actual_surface_skips_lines_without_two_floats(tmp_path: Path) -> None:
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

    points = parse_scan_actual_surface(out_path)

    assert [point.index for point in points] == [1, 2]
    assert points[1].energy == pytest.approx(-99.5)


def test_strict_scan_coordinate_rejects_unclosed_geom_nesting() -> None:
    with pytest.raises(ValueError, match="closed"):
        validate_scan_coordinate_lines(
            ["%geom", "  Scan", "    B 0 1 = 1.2, 3.0, 10", "  end"],
            atom_count=2,
        )


def test_strict_scan_coordinate_rejects_duplicate_active_geom_blocks() -> None:
    lines = _scan_lines()
    lines[lines.index("* xyzfile 0 1 input.xyz") : lines.index("* xyzfile 0 1 input.xyz")] = [
        "%geom",
        "  MaxIter 50",
        "end",
    ]

    with pytest.raises(ValueError, match="exactly one active %geom"):
        validate_scan_coordinate_lines(lines, atom_count=32)


def test_format_scan_float_normalizes_negative_zero() -> None:
    assert _format_scan_float(-0.0) == "0"
    assert _format_scan_float(-1e-9) == "-1e-09"
    assert _format_scan_float(1.86) == "1.86"


def test_parse_scan_actual_surface_stops_at_the_timing_section(tmp_path: Path) -> None:
    # An output without the SCF-energy table used to keep reading: the
    # timing lines carry two numbers and became points 4 and 5.
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   1.91000000 -99.50000000",
                "   1.96000000 -99.80000000",
                "",
                "Timings for individual modules:",
                "Sum of individual times         ...    27530.368 sec (=  458.839 min)",
                "TOTAL RUN TIME: 0 days 1 hours 2 minutes 3 seconds 4 msec",
                "",
            ]
        ),
        encoding="utf-8",
    )

    points = parse_scan_actual_surface(out_path)

    assert [point.index for point in points] == [1, 2, 3]
    assert max(point.energy for point in points) == pytest.approx(-99.5)


def test_parse_scan_actual_surface_refuses_rows_that_are_not_energies(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    out_path.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                "   1.86000000 -100.00000000",
                "   0 1 2 3 4.0",
                "   1.91000000 458.839",
                "   1.96000000 -99.80000000",
                "The Calculated Surface using the SCF energy",
                "   1.86000000 -101.00000000",
            ]
        ),
        encoding="utf-8",
    )

    points = parse_scan_actual_surface(out_path)

    assert [(point.index, point.coordinates, point.energy) for point in points] == [
        (1, (1.86,), -100.0),
        (4, (1.96,), -99.8),
    ]
    # The step number of the refused rows is kept, so the highest point still
    # names the geometry file ORCA wrote for that step.
    assert points[1].index == 4


@pytest.mark.parametrize(
    "unprintable_row",
    [
        # All 652 actual-energy rows in this lab's 36 real ORCA outputs are 28
        # columns: three blanks, a 10-column coordinate, one blank, a 14-column
        # energy. An energy field that overflows fills its own width with
        # asterisks, so if that blank is the energy field's own padding the
        # asterisks abut the coordinate (first shape) and if it is a literal
        # separator they do not (second shape). Finished rows cannot tell the
        # two layouts apart, so both are pinned. No asterisk appears inside any
        # actual-energy section in the corpus, so nothing on disk settles it.
        "   1.91000000***************",
        "   1.91000000 **************",
        "   1.91000000            NaN",
        "   1.91000000      -Infinity",
    ],
)
def test_parse_scan_actual_surface_counts_a_step_it_could_not_print(
    tmp_path: Path,
    unprintable_row: str,
) -> None:
    # ORCA wrote `rxn.002.xyz` for the step whose energy it could not print, so
    # dropping the row must not renumber every step after it.
    out_path = tmp_path / "rxn.out"
    _write_surface_out(
        out_path,
        [
            "   1.86000000 -100.00000000",
            unprintable_row,
            "   1.96000000 -99.80000000",
            "   2.01000000 -99.70000000",
        ],
    )

    points = parse_scan_actual_surface(out_path)

    assert [point.index for point in points] == [1, 3, 4]
    assert points[-1].coordinates == (2.01,)


def test_parse_scan_actual_surface_counts_a_row_with_a_spoiled_coordinate(
    tmp_path: Path,
) -> None:
    # The rule does not care which column was lost, and this pins that. How
    # reachable the shape is, stated honestly: this lab's outputs hold only
    # bond scans, coordinates 0.59 to 4.0 Å, so no coordinate ORCA could not
    # print appears on disk. Whether an angle or dihedral scan could overflow
    # the coordinate field depends on a field width that finished rows do not
    # reveal, so this is not asserted to be unreachable — only unobserved.
    out_path = tmp_path / "rxn.out"
    _write_surface_out(
        out_path,
        [
            "   1.86000000 -100.00000000",
            "   ******* -99.50000000",
            "   1.96000000 -99.80000000",
            "   2.01000000 -99.70000000",
        ],
    )

    points = parse_scan_actual_surface(out_path)

    assert [point.index for point in points] == [1, 3, 4]
    assert points[-1].coordinates == (2.01,)


@pytest.mark.parametrize("rule_line", ["***** *****", "***************************"])
def test_parse_scan_actual_surface_treats_an_asterisk_rule_line_as_prose(
    tmp_path: Path,
    rule_line: str,
) -> None:
    # A step has to show at least one readable number. Lines of four or more
    # asterisks run to 387,179 across this lab's 792 outputs, and counting one
    # as a step would reintroduce the off-by-one this rule exists to remove.
    out_path = tmp_path / "rxn.out"
    _write_surface_out(
        out_path,
        [
            "   1.86000000 -100.00000000",
            rule_line,
            "   1.91000000 -99.50000000",
        ],
    )

    points = parse_scan_actual_surface(out_path)

    assert [point.index for point in points] == [1, 2]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # The defect: the leading row is not an energy row and carries an extra
        # column, and fixing the table width on it refused every genuine row
        # that followed — the whole table was lost.
        pytest.param(
            [
                "   1 1.86000000 458.839",
                "   1.91000000 -100.00000000",
                "   1.96000000 -99.80000000",
            ],
            [(2, (1.91,)), (3, (1.96,))],
            id="malformed_leading_row_no_longer_refuses_the_table",
        ),
        # Nothing valid matches the leading row's width and the valid rows
        # disagree with each other, so the width the majority shares wins
        # rather than whichever valid row came first.
        pytest.param(
            [
                "   1 2 1.86000000 458.839",
                "   1 1.90000000 -100.00000000",
                "   1.94000000 -99.90000000",
                "   1.98000000 -99.80000000",
            ],
            [(3, (1.94,)), (4, (1.98,))],
            id="majority_width_when_nothing_valid_matches_the_leading_row",
        ),
        # Regression lock, green before this change too: the leading row is
        # refused for its energy, not its width, and the genuine rows share
        # that width. Taking the first *valid* row's width instead would keep
        # the three-column row and discard both genuine points, handing a
        # continuation scan the wrong starting structure.
        pytest.param(
            [
                "   1.80000000 458.83900000",
                "   1 1.86000000 -1540.90000000",
                "   1.92000000 -1540.80000000",
                "   1.98000000 -1540.70000000",
            ],
            [(3, (1.92,)), (4, (1.98,))],
            id="a_refused_leading_row_still_fixes_the_width",
        ),
        # Regression lock: a valid leading row keeps fixing the width even when
        # most of the valid rows are a different shape, so no table that parses
        # today parses differently.
        pytest.param(
            [
                "   1 1.86000000 -1540.90000000",
                "   1.92000000 -1540.80000000",
                "   1.98000000 -1540.70000000",
            ],
            [(1, (1.0, 1.86))],
            id="the_leading_row_width_beats_the_majority",
        ),
    ],
)
def test_parse_scan_actual_surface_fixes_the_table_width(
    tmp_path: Path,
    rows: list[str],
    expected: list[tuple[int, tuple[float, ...]]],
) -> None:
    out_path = tmp_path / "rxn.out"
    _write_surface_out(out_path, rows)

    points = parse_scan_actual_surface(out_path)

    assert [(point.index, point.coordinates) for point in points] == expected


def test_scan_profile_interior_barrier_prominence() -> None:
    kcal_per_hartree = 627.5094740631

    assert scan_profile_interior_barrier_kcal([-100.0, -99.9]) is None
    assert scan_profile_interior_barrier_kcal([-100.0, -100.1, -100.2, -100.3]) == pytest.approx(
        0.0
    )
    # Maximum at the profile edge is not an interior barrier.
    assert scan_profile_interior_barrier_kcal([-99.9, -100.0, -100.1]) == pytest.approx(0.0)
    # Interior hump of 0.00015 Ha above the shallower flank.
    noise = scan_profile_interior_barrier_kcal([-100.0, -99.99985, -100.0002, -100.001])
    assert noise == pytest.approx(0.00015 * kcal_per_hartree, rel=1e-6)
    barrier = scan_profile_interior_barrier_kcal([-100.0, -99.99, -100.02])
    assert barrier == pytest.approx(0.01 * kcal_per_hartree, rel=1e-6)


def _write_surface_out(path: Path, rows: list[str]) -> None:
    path.write_text(
        "\n".join(
            [
                "The Calculated Surface using the 'Actual Energy'",
                *rows,
                "The Calculated Surface using the SCF energy",
                "   1.86000000 -101.00000000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
