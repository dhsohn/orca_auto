from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from .completion_rules import CompletionMode
from .frequencies import scan_frequency_sections
from .output_status import (
    is_execution_output_line,
    optimization_convergence_line,
    scf_convergence_line,
    termination_line,
)
from .parser.io import open_orca_text
from .parser.patterns import FINAL_SINGLE_POINT_ENERGY_RE, final_single_point_energy_value
from .statuses import AnalyzerStatus

logger = logging.getLogger(__name__)

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
    # Whether the count comes from the final frequency section. This is
    # observed evidence, not by itself a successful calculation or a minimum.
    final_frequency_section: bool
    energy_hartree: float | None
    scf_converged: bool | None
    energy_line: int | None
    optimization_line: int | None
    scf_line: int | None


# Evidence that ORCA's IRC driver ran. The analyzer looks for the upper-case
# IRC_PATH_FOUND_NEEDLES in each execution output line. The IRC report's badge
# instead searches its whole raw text, input echoes included, for the driver
# needle or IRC_PATH_SUMMARY_RE (whole words, any case, any whitespace between
# them), the header it also reads the path summary table under.
IRC_DRIVER_NEEDLE = "IRC-DRV"
IRC_PATH_FOUND_NEEDLES = ("IRC PATH SUMMARY", IRC_DRIVER_NEEDLE)
IRC_PATH_SUMMARY_RE = re.compile(r"\bIRC\s+PATH\s+SUMMARY\b", re.IGNORECASE)

_MARKER_RULES: tuple[tuple[BooleanMarkerName, tuple[str, ...]], ...] = (
    ("total_run_time_seen", ("TOTAL RUN TIME",)),
    ("irc_marker_found", IRC_PATH_FOUND_NEEDLES),
    ("scfgrad_abort", ("ORCA FINISHED BY ERROR TERMINATION IN SCF GRADIENT",)),
    ("disk_io_error", ("COULD NOT WRITE TO DISK", "NO SPACE LEFT ON DEVICE")),
    ("ts_failure_marker", ("NO ACCEPTABLE TS", "FAILED TO FIND TS")),
    (
        "geometry_zero_distance",
        ("ZERO DISTANCE ENCOUNTERED", "ZERO DISTANCE BETWEEN ATOMS"),
    ),
)


_MEMORY_ERROR_NEEDLES = ("OUT OF MEMORY", "INSUFFICIENT MEMORY", "CANNOT ALLOCATE MEMORY")
# ORCA 6 memory advisories after which the run continues: "WARNING [...]: Out of
# memory according to MaxCore limit!" and the LOW MEMORY block's closing line
# "INSUFFICIENT MEMORY MAY LEAD TO SLOW PERFORMANCE OR CRASHES.".
_MEMORY_ADVISORY_NEEDLES = ("WARNING", "MAY LEAD TO")


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
        "energy_hartree": None,
        "scf_converged": None,
        "energy_line": None,
        "optimization_line": None,
        "scf_line": None,
    }


def _scan_line_for_markers(line: str, markers: OutMarkers, line_number: int) -> None:
    if not is_execution_output_line(line):
        return
    upper = line.upper()
    scf = scf_convergence_line(line)
    if scf is not None:
        markers["scf_converged"] = scf
        markers["scf_error"] = not scf
        markers["scf_line"] = line_number
    if upper.lstrip().startswith("FINAL SINGLE POINT ENERGY"):
        markers["energy_hartree"] = None
        markers["energy_line"] = line_number
        match = FINAL_SINGLE_POINT_ENERGY_RE.fullmatch(line.rstrip("\r\n"))
        if match is not None and match.group(2) is None:
            try:
                markers["energy_hartree"] = final_single_point_energy_value(match.group(1))
            except ValueError:
                pass
        elif match is not None:
            markers["scf_converged"] = False
            markers["scf_error"] = True
            markers["scf_line"] = line_number
    normal, error = termination_line(line)
    markers["terminated_normally"] |= normal
    markers["generic_error_termination"] |= error
    if "MULTIPLICITY" in upper and "IMPOSSIBLE" in upper:
        markers["multiplicity_impossible"] = True
    if any(needle in upper for needle in _MEMORY_ERROR_NEEDLES) and not any(
        needle in upper for needle in _MEMORY_ADVISORY_NEEDLES
    ):
        markers["memory_error"] = True
    verdict = optimization_convergence_line(line)
    if verdict is not None:
        markers["last_opt_converged"] = verdict
        markers["optimization_line"] = line_number
        if verdict:
            markers["opt_converged"] = True
        else:
            markers["geom_not_converged"] = True
    for marker_name, needles in _MARKER_RULES:
        if any(needle in upper for needle in needles):
            markers[marker_name] = True


def _marked_lines(lines: Iterable[str], markers: OutMarkers) -> Iterator[str]:
    """``lines`` passed through unchanged, each scanned for the diagnostic markers."""
    for line_number, line in enumerate(lines, start=1):
        _scan_line_for_markers(line, markers, line_number)
        yield line


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
        missing = ""
        if markers["energy_hartree"] is None:
            missing = "energy_evidence_missing"
        elif mode.kind in {"opt", "ts"} and markers["last_opt_converged"] is not True:
            missing = "optimization_evidence_missing"
        elif (mode.require_frequency or mode.kind == "ts") and not markers[
            "final_frequency_section"
        ]:
            missing = "frequency_evidence_missing"
        elif mode.require_irc and not markers["irc_marker_found"]:
            missing = "irc_evidence_missing"
        if missing:
            return OutAnalysis(status=AnalyzerStatus.INCOMPLETE, reason=missing, markers=markers)
        if mode.kind == "ts":
            if markers["imaginary_frequency_count"] != 1:
                return OutAnalysis(
                    status=AnalyzerStatus.TS_NOT_FOUND, reason="ts_criteria_failed", markers=markers
                )
            return OutAnalysis(
                status=AnalyzerStatus.COMPLETED, reason="ts_criteria_met", markers=markers
            )
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
        if markers[marker_name]:
            return OutAnalysis(status=status, reason=reason, markers=markers)
    if markers["last_opt_converged"] is False:
        return OutAnalysis(
            status=AnalyzerStatus.GEOM_NOT_CONVERGED,
            reason="geometry_not_converged",
            markers=markers,
        )
    return None


def analyze_output(
    out_path: Path, mode: CompletionMode, *, byte_observer: Callable[[bytes], None] | None = None
) -> OutAnalysis:
    markers = _default_markers(out_path)
    logger.debug("Analyzing output: %s (mode=%s)", out_path, mode.kind)
    if not out_path.exists():
        return OutAnalysis(
            status=AnalyzerStatus.INCOMPLETE, reason="output_missing", markers=markers
        )

    try:
        with open_orca_text(out_path, byte_observer=byte_observer) as handle:
            sections = scan_frequency_sections(_marked_lines(handle, markers))
    except OSError:
        return OutAnalysis(
            status=AnalyzerStatus.INCOMPLETE, reason="output_read_error", markers=markers
        )
    if sections.analysis is not None:
        markers["imaginary_frequency_count"] = sections.analysis.imaginary_count()
        markers["final_frequency_section"] = True
    return _interpret_markers(markers, mode)


def apply_exit_code(analysis: OutAnalysis, return_code: int) -> OutAnalysis:
    """Reconcile the verdict with the ORCA process exit code.

    A specific analyzer failure stands, but success is never published over a
    failed process: a completed-looking output of a nonzero exit becomes
    ``nonzero_exit_code``, observed evidence remains available for diagnosis.
    """
    if analysis.status != AnalyzerStatus.COMPLETED or return_code == 0:
        return analysis
    markers = analysis.markers.copy()
    return OutAnalysis(
        status=AnalyzerStatus.UNKNOWN_FAILURE,
        reason="nonzero_exit_code",
        markers=markers,
    )
