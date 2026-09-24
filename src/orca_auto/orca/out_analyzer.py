from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from .completion_rules import CompletionMode
from .frequencies import frequency_values, is_imaginary_frequency, scan_frequency_sections
from .output_status import (
    is_execution_output_line,
    iter_output_lines,
    optimization_convergence_line,
    termination_line,
)
from .parser.io import open_orca_text, read_orca_text
from .statuses import AnalyzerStatus

logger = logging.getLogger(__name__)

_DEFAULT_BUFFER_BYTES = 64 * 1024
_TS_BUFFER_BYTES = 256 * 1024

BooleanMarkerName = Literal[
    "terminated_normally",
    "total_run_time_seen",
    "irc_marker_found",
    "opt_converged",
    "scf_error",
    "scfgrad_abort",
    "multiplicity_impossible",
    "disk_io_error",
    "generic_error_termination",
    "ts_failure_marker",
    "memory_error",
    "geometry_zero_distance",
    "geom_not_converged",
]


class OutMarkers(TypedDict):
    out_path: str
    terminated_normally: bool
    imaginary_frequency_count: int
    irc_marker_found: bool
    opt_converged: bool
    scf_error: bool
    scfgrad_abort: bool
    multiplicity_impossible: bool
    disk_io_error: bool
    generic_error_termination: bool
    ts_failure_marker: bool
    memory_error: bool
    geometry_zero_distance: bool
    geom_not_converged: bool
    last_opt_converged: bool | None
    total_run_time_seen: bool
    # True when ``imaginary_frequency_count`` is a verdict on the final
    # geometry: it was counted in a frequency section not followed by another
    # final single point energy and the analyzer reached the TS criteria.
    # False for a superseded-only output, for the legacy whole-file count,
    # for non-TS modes, and for runs that ended in a geometry or SCF failure.
    final_frequency_section: bool


_MARKER_RULES: tuple[tuple[BooleanMarkerName, tuple[str, ...]], ...] = (
    ("total_run_time_seen", ("TOTAL RUN TIME",)),
    ("irc_marker_found", ("IRC PATH SUMMARY", "IRC-DRV")),
    ("scf_error", ("SCF NOT CONVERGED", "SCF CONVERGENCE FAILED")),
    ("scfgrad_abort", ("ORCA FINISHED BY ERROR TERMINATION IN SCF GRADIENT",)),
    ("disk_io_error", ("COULD NOT WRITE TO DISK", "NO SPACE LEFT ON DEVICE")),
    ("ts_failure_marker", ("NO ACCEPTABLE TS", "FAILED TO FIND TS")),
    ("memory_error", ("OUT OF MEMORY", "INSUFFICIENT MEMORY", "CANNOT ALLOCATE MEMORY")),
    (
        "geometry_zero_distance",
        ("ZERO DISTANCE ENCOUNTERED", "ZERO DISTANCE BETWEEN ATOMS"),
    ),
)


@dataclass
class OutAnalysis:
    status: AnalyzerStatus
    reason: str
    markers: OutMarkers


def _default_markers(out_path: Path) -> OutMarkers:
    return {
        "out_path": str(out_path),
        "terminated_normally": False,
        "imaginary_frequency_count": 0,
        "irc_marker_found": False,
        "opt_converged": False,
        "scf_error": False,
        "scfgrad_abort": False,
        "multiplicity_impossible": False,
        "disk_io_error": False,
        "generic_error_termination": False,
        "ts_failure_marker": False,
        "memory_error": False,
        "geometry_zero_distance": False,
        "geom_not_converged": False,
        "last_opt_converged": None,
        "total_run_time_seen": False,
        "final_frequency_section": False,
    }


def _marker_payload(markers: OutMarkers) -> dict[str, Any]:
    return cast(dict[str, Any], markers)


def _set_marker(markers: OutMarkers, marker_name: BooleanMarkerName) -> None:
    _marker_payload(markers)[marker_name] = True


def _marker_is_set(markers: OutMarkers, marker_name: BooleanMarkerName) -> bool:
    return bool(_marker_payload(markers).get(marker_name))


def _scan_line_for_markers(line: str, markers: OutMarkers) -> None:
    if not is_execution_output_line(line):
        return
    upper = line.upper()
    normal, error = termination_line(line)
    markers["terminated_normally"] |= normal
    markers["generic_error_termination"] |= error
    if "MULTIPLICITY" in upper and "IMPOSSIBLE" in upper:
        markers["multiplicity_impossible"] = True
    verdict = optimization_convergence_line(line)
    if verdict is not None:
        markers["last_opt_converged"] = verdict
        _set_marker(markers, "opt_converged" if verdict else "geom_not_converged")
    for marker_name, needles in _MARKER_RULES:
        if any(needle in upper for needle in needles):
            _set_marker(markers, marker_name)


def _scan_text_for_markers(text: str, markers: OutMarkers) -> None:
    for line in iter_output_lines(text):
        _scan_line_for_markers(line, markers)


def _interpret_markers(markers: OutMarkers, mode: CompletionMode) -> OutAnalysis:
    error_analysis = _marker_error_analysis(markers)
    if error_analysis is not None:
        return error_analysis

    if (
        mode.kind == "ts"
        and markers["ts_failure_marker"]
        and (not markers["terminated_normally"] or markers["generic_error_termination"])
    ):
        return OutAnalysis(
            status=AnalyzerStatus.TS_NOT_FOUND, reason="ts_failure_marker", markers=markers
        )

    if markers["generic_error_termination"]:
        return OutAnalysis(
            status=AnalyzerStatus.UNKNOWN_FAILURE, reason="error_termination", markers=markers
        )

    if markers["terminated_normally"]:
        if mode.kind == "ts":
            return _interpret_ts_completion(markers, mode)
        return OutAnalysis(
            status=AnalyzerStatus.COMPLETED, reason="normal_termination", markers=markers
        )

    return OutAnalysis(status=AnalyzerStatus.INCOMPLETE, reason="run_incomplete", markers=markers)


def _marker_error_analysis(markers: OutMarkers) -> OutAnalysis | None:
    checks: tuple[tuple[BooleanMarkerName, AnalyzerStatus, str], ...] = (
        (
            "multiplicity_impossible",
            AnalyzerStatus.ERROR_MULTIPLICITY_IMPOSSIBLE,
            "multiplicity_parity_mismatch",
        ),
        ("disk_io_error", AnalyzerStatus.ERROR_DISK_IO, "disk_write_failed"),
        ("memory_error", AnalyzerStatus.ERROR_MEMORY, "out_of_memory"),
        (
            "geometry_zero_distance",
            AnalyzerStatus.ERROR_GEOMETRY,
            "geometry_zero_distance",
        ),
        ("scfgrad_abort", AnalyzerStatus.ERROR_SCFGRAD_ABORT, "scf_gradient_abort"),
        ("scf_error", AnalyzerStatus.ERROR_SCF, "scf_not_converged"),
    )
    for marker_name, status, reason in checks:
        if _marker_is_set(markers, marker_name):
            return OutAnalysis(status=status, reason=reason, markers=markers)
    if markers["last_opt_converged"] is False:
        return OutAnalysis(
            status=AnalyzerStatus.GEOM_NOT_CONVERGED,
            reason="geometry_not_converged",
            markers=markers,
        )
    return None


def _interpret_ts_completion(markers: OutMarkers, mode: CompletionMode) -> OutAnalysis:
    imag_ok = markers["imaginary_frequency_count"] == 1
    irc_ok = (not mode.require_irc) or bool(markers["irc_marker_found"])
    if imag_ok and irc_ok:
        return OutAnalysis(
            status=AnalyzerStatus.COMPLETED, reason="ts_criteria_met", markers=markers
        )
    return OutAnalysis(
        status=AnalyzerStatus.TS_NOT_FOUND, reason="ts_criteria_failed", markers=markers
    )


def _scan_full_for_markers(out_path: Path, markers: OutMarkers) -> None:
    """Stream complete lines with the same diagnostic rules as buffered reads."""
    with open_orca_text(out_path) as handle:
        for line in handle:
            _scan_line_for_markers(line, markers)


def scan_ts_lines_for_imag_count(lines: Iterable[str]) -> tuple[int, bool]:
    """Imaginary modes of the frequency section that verifies the final geometry.

    ORCA prints a ``VIBRATIONAL FREQUENCIES`` section for every Hessian it
    computes, including the initial and recalculated Hessians of an ``OptTS``
    run. A section followed by another final single point energy belongs to an
    earlier geometry and verifies nothing; only a section after the last final
    energy counts. Section selection, the frequency line rule and the noise
    threshold are ``frequencies.scan_frequency_sections``: the count is the
    ``imaginary_count()`` of the very analysis the SI and reports publish, so
    the verdict and the published Nimag cannot disagree. An output whose
    sections were all superseded reports zero modes, and an output without any
    section keeps the legacy whole-file count of imaginary wavenumbers.

    Returns ``(imaginary_count, final_section)`` where ``final_section`` is
    True only when the count came from a section after the last final energy.

    Every caller feeds the same universal-newline line stream, so the count
    does not depend on whether the file was small enough to be read whole.
    """
    headerless_count = 0

    def counted_lines() -> Iterator[str]:
        nonlocal headerless_count
        for line in lines:
            if is_execution_output_line(line):
                headerless_count += sum(
                    1 for value in frequency_values(line) if is_imaginary_frequency(value)
                )
            yield line

    sections = scan_frequency_sections(counted_lines())
    if sections.analysis is not None:
        return sections.analysis.imaginary_count(), True
    if sections.seen:
        return 0, False
    return headerless_count, False


def _scan_ts_full_for_imag_count(out_path: Path) -> tuple[int, bool]:
    with open_orca_text(out_path) as handle:
        return scan_ts_lines_for_imag_count(handle)


def _scan_ts_text_for_imag_count(text: str) -> tuple[int, bool]:
    # The shared iterator splits exactly where reading the file
    # would: on LF, CR and CRLF only. ``str.splitlines()`` also breaks on the
    # vertical tab, the form feed, the file/group/record separators, NEL, and
    # the Unicode line and paragraph separators, so a small output holding any
    # of those would be sectioned differently from the same output read past
    # the tail window.
    return scan_ts_lines_for_imag_count(iter_output_lines(text))


def analyze_output(out_path: Path, mode: CompletionMode) -> OutAnalysis:
    markers = _default_markers(out_path)
    logger.debug("Analyzing output: %s (mode=%s)", out_path, mode.kind)
    if not out_path.exists():
        return OutAnalysis(
            status=AnalyzerStatus.INCOMPLETE, reason="output_missing", markers=markers
        )

    try:
        file_size = out_path.stat().st_size
        buffer_bytes = _TS_BUFFER_BYTES if mode.kind == "ts" else _DEFAULT_BUFFER_BYTES
        full_text: str | None = None

        # Both branches decode by the parser's rule (``parser.io``), so the
        # verdict reads the same text as the frequency analysis and the reports.
        if file_size <= buffer_bytes:
            # Buffer small files so TS verification can reuse the same text.
            full_text = read_orca_text(out_path)
            _scan_text_for_markers(full_text, markers)
        else:
            _scan_full_for_markers(out_path, markers)

        # TS mode needs exact imaginary frequency count from the final vibration block.
        if mode.kind == "ts" and markers["terminated_normally"]:
            if full_text is None:
                imag_count, final_section = _scan_ts_full_for_imag_count(out_path)
            else:
                imag_count, final_section = _scan_ts_text_for_imag_count(full_text)
            markers["imaginary_frequency_count"] = imag_count
            markers["final_frequency_section"] = final_section

    except OSError:
        return OutAnalysis(
            status=AnalyzerStatus.INCOMPLETE, reason="output_read_error", markers=markers
        )

    analysis = _interpret_markers(markers, mode)
    if analysis.reason not in ("ts_criteria_met", "ts_criteria_failed"):
        # The count is a verdict on the final geometry only when the analyzer
        # reached the TS criteria. A normally terminated run whose geometry
        # did not converge or whose SCF failed keeps its count for diagnostics
        # but does not characterize a stationary point.
        markers["final_frequency_section"] = False
    return analysis
