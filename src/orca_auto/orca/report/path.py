"""Path-profile pieces shared by the IRC and NEB reports.

Both drivers print a path summary (``IRC PATH SUMMARY`` / ``PATH SUMMARY FOR
NEB-TS``) as rows of ``label  E(Eh)  dE(kcal/mol)  max(|G|)  RMS(G) [<= MARKER]``
and an iteration table under direction/phase banners. The parsers and
renderers here are parameterised by those banners and column labels; the
report modules keep the driver-specific regexes, settings loops, and cards.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TypeVar

from .render import ChartSeries, line_chart_svg

_NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?"


@dataclass(frozen=True)
class PathPoint:
    """One row of a path summary.

    ``position`` is the printed step (IRC) or image index (NEB) and ``None``
    for the ``TS`` row; ``order`` is the row's place in the table.
    """

    label: str
    order: int
    position: int | None
    energy_hartree: float
    relative_kcal: float
    max_gradient: float
    rms_gradient: float
    marker: str


@dataclass(frozen=True)
class IrcPathPoint(PathPoint):
    @property
    def step(self) -> int | None:
        return self.position


@dataclass(frozen=True)
class NebPathPoint(PathPoint):
    @property
    def image_index(self) -> int | None:
        return self.position

    @property
    def max_force(self) -> float:
        return self.max_gradient

    @property
    def rms_force(self) -> float:
        return self.rms_gradient


P = TypeVar("P", bound=PathPoint)


def path_summary_row_re(label_pattern: str) -> re.Pattern[str]:
    """Row regex: label, E, ΔE, max gradient, RMS gradient, optional ``<= MARKER``."""
    return re.compile(
        rf"^\s*({label_pattern})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})"
        r"(?:\s+<=\s*([A-Za-z-]+))?\s*$",
        re.IGNORECASE,
    )


def parse_path_summary(
    text: str,
    *,
    header_re: re.Pattern[str],
    row_re: re.Pattern[str],
    point_type: type[P],
) -> tuple[P, ...]:
    """Rows of the last path summary in ``text``; a later summary replaces an earlier one."""
    points: list[P] = []
    in_summary = False
    for line in text.splitlines():
        if header_re.search(line):
            in_summary = True
            points = []
            continue
        if not in_summary:
            continue
        match = row_re.match(line)
        if match is None:
            if points and not line.strip():
                in_summary = False
            continue
        label = match.group(1).upper()
        points.append(
            point_type(
                label=label,
                order=len(points),
                position=None if label == "TS" else int(label),
                energy_hartree=float(match.group(2)),
                relative_kcal=float(match.group(3)),
                max_gradient=float(match.group(4)),
                rms_gradient=float(match.group(5)),
                marker=(match.group(6) or "").upper(),
            )
        )
    return tuple(points)


def iter_phase_table_rows(
    text: str,
    *,
    phase_of_line: Callable[[str], str | None],
    row_re: re.Pattern[str],
) -> Iterator[tuple[str, re.Match[str]]]:
    """``(phase, row match)`` for iteration tables printed under phase banners.

    ``phase_of_line`` names the phase a banner line opens (IRC direction, NEB
    HEI/CI header), ``""`` for a line that ends the current phase, and ``None``
    for any other line. A table ends at the first non-blank, non-row line after
    its first row.
    """
    phase = ""
    table_started = False
    for line in text.splitlines():
        new_phase = phase_of_line(line)
        if new_phase is not None:
            phase = new_phase
            table_started = False
            continue
        if not phase:
            continue
        match = row_re.match(line)
        if match is None and table_started and line.strip():
            phase = ""
            table_started = False
            continue
        if match is None:
            continue
        table_started = True
        yield phase, match


def attempt_detail_text(path_points: Sequence[PathPoint], *parts: str) -> str:
    """Attempt-chain detail cell: ``"N path pts"`` plus the non-empty driver ``parts``."""
    cells = [f"{len(path_points)} path pts"] if path_points else []
    cells.extend(part for part in parts if part)
    return ", ".join(cells)


def path_marker_index(points: Sequence[PathPoint], marker: str) -> int | None:
    """Index of the first point whose ``marker`` or ``label`` equals ``marker``."""
    marker = marker.upper()
    for index, point in enumerate(points):
        if point.marker == marker or point.label == marker:
            return index
    return None


def path_table_html(
    points: Sequence[PathPoint],
    columns: Sequence[tuple[str, Callable[[PathPoint], str]]],
) -> str:
    """Path table with the given ``(header, cell)`` columns plus the marker column."""
    if not points:
        return ""
    rows = []
    for point in points:
        marker = f"<= {html.escape(point.marker)}" if point.marker else ""
        cells = "".join(f"<td>{cell(point)}</td>" for _header, cell in columns)
        rows.append(f"<tr>{cells}<td>{marker}</td></tr>")
    headers = "".join(f"<th>{header}</th>" for header, _cell in columns)
    return (
        f"<table><thead><tr>{headers}<th>Marker</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def path_profile_chart_svg(
    points: Sequence[PathPoint],
    *,
    x_of: Callable[[int], float],
    path_label: str,
    highlights: Sequence[tuple[str, str, int | None]],
    x_label: str,
    y_label: str,
    x_ticks: Sequence[float] | None = None,
) -> str:
    """Relative-energy profile with single-point ``(label, color, index)`` highlights."""
    if len(points) < 2:
        return ""
    series = [
        ChartSeries(
            label=path_label,
            color="#2f6fb2",
            dash="",
            points=tuple((x_of(index), point.relative_kcal) for index, point in enumerate(points)),
        )
    ]
    for label, color, index in highlights:
        if index is None:
            continue
        series.append(
            ChartSeries(
                label=label,
                color=color,
                dash="",
                points=((x_of(index), points[index].relative_kcal),),
            )
        )
    return line_chart_svg(
        tuple(series),
        x_label=x_label,
        y_label=y_label,
        x_tick_fmt=".0f",
        x_ticks=x_ticks,
    )


__all__ = [
    "IrcPathPoint",
    "NebPathPoint",
    "PathPoint",
    "attempt_detail_text",
    "iter_phase_table_rows",
    "parse_path_summary",
    "path_marker_index",
    "path_profile_chart_svg",
    "path_summary_row_re",
    "path_table_html",
]
