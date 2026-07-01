from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, TypedDict, cast

from .completion_rules import CompletionMode
from .output_status import ERROR_TERMINATION_NEEDLES, NORMAL_TERMINATION_NEEDLES
from .statuses import AnalyzerStatus

logger = logging.getLogger(__name__)


NEG_FREQ_RE = re.compile(r"(^|\s)(-\d+\.\d+)\s*cm\*\*-1", re.IGNORECASE)
VIB_FREQ_HEADER = "VIBRATIONAL FREQUENCIES"

_DEFAULT_TAIL_BYTES = 64 * 1024
_TS_TAIL_BYTES = 256 * 1024
_HEAD_BYTES = 8 * 1024

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
    total_run_time_seen: bool


_MARKER_RULES: tuple[tuple[BooleanMarkerName, tuple[str, ...]], ...] = (
    ("terminated_normally", NORMAL_TERMINATION_NEEDLES),
    ("total_run_time_seen", ("TOTAL RUN TIME",)),
    ("irc_marker_found", ("IRC PATH SUMMARY", "IRC-DRV")),
    ("opt_converged", ("THE OPTIMIZATION HAS CONVERGED", "OPTIMIZATION RUN DONE")),
    ("scf_error", ("SCF NOT CONVERGED", "SCF CONVERGENCE FAILED")),
    ("scfgrad_abort", ("ORCA FINISHED BY ERROR TERMINATION IN SCF GRADIENT",)),
    ("disk_io_error", ("COULD NOT WRITE TO DISK", "NO SPACE LEFT ON DEVICE")),
    ("generic_error_termination", ERROR_TERMINATION_NEEDLES),
    ("ts_failure_marker", ("NO ACCEPTABLE TS", "FAILED TO FIND TS")),
    ("memory_error", ("OUT OF MEMORY", "INSUFFICIENT MEMORY", "CANNOT ALLOCATE MEMORY")),
    (
        "geometry_zero_distance",
        ("ZERO DISTANCE ENCOUNTERED", "ZERO DISTANCE BETWEEN ATOMS"),
    ),
    (
        "geom_not_converged",
        ("THE OPTIMIZATION DID NOT CONVERGE", "OPTIMIZATION HAS NOT YET CONVERGED"),
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
        "total_run_time_seen": False,
    }


def _marker_payload(markers: OutMarkers) -> dict[str, Any]:
    return cast(dict[str, Any], markers)


def _set_marker(markers: OutMarkers, marker_name: BooleanMarkerName) -> None:
    _marker_payload(markers)[marker_name] = True


def _marker_is_set(markers: OutMarkers, marker_name: BooleanMarkerName) -> bool:
    return bool(_marker_payload(markers).get(marker_name))


def _scan_line_for_markers(line: str, markers: OutMarkers) -> None:
    upper = line.upper()
    if "MULTIPLICITY" in upper and "IMPOSSIBLE" in upper:
        markers["multiplicity_impossible"] = True
    for marker_name, needles in _MARKER_RULES:
        if any(needle in upper for needle in needles):
            _set_marker(markers, marker_name)


def _scan_text_for_markers(text: str, markers: OutMarkers) -> None:
    for line in text.splitlines():
        _scan_line_for_markers(line, markers)


def _interpret_markers(markers: OutMarkers, mode: CompletionMode) -> OutAnalysis:
    error_analysis = _marker_error_analysis(markers)
    if error_analysis is not None:
        return error_analysis

    if markers["terminated_normally"]:
        if mode.kind == "ts":
            return _interpret_ts_completion(markers, mode)
        return OutAnalysis(
            status=AnalyzerStatus.COMPLETED, reason="normal_termination", markers=markers
        )

    if mode.kind == "ts" and markers["ts_failure_marker"]:
        return OutAnalysis(
            status=AnalyzerStatus.TS_NOT_FOUND, reason="ts_failure_marker", markers=markers
        )

    if markers["generic_error_termination"]:
        return OutAnalysis(
            status=AnalyzerStatus.UNKNOWN_FAILURE, reason="error_termination", markers=markers
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
    if markers["geom_not_converged"] and not markers["terminated_normally"]:
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


def _read_tail(out_path: Path, encoding: str, nbytes: int) -> str:
    file_size = out_path.stat().st_size
    offset = max(0, file_size - nbytes)
    with out_path.open("rb") as fh:
        if offset > 0:
            fh.seek(offset)
        raw = fh.read()
    return raw.decode(encoding, errors="ignore")


def _read_head(out_path: Path, encoding: str, nbytes: int) -> str:
    with out_path.open("rb") as fh:
        raw = fh.read(nbytes)
    return raw.decode(encoding, errors="ignore")


def _scan_ts_lines_for_imag_count(lines: Iterable[str]) -> tuple[int, bool]:
    total_negative_count = 0
    last_vib_section_negative_count = 0
    saw_vib_section = False
    irc_found = False

    for line in lines:
        upper = line.upper()
        if "IRC PATH SUMMARY" in upper or "IRC-DRV" in upper:
            irc_found = True
        if VIB_FREQ_HEADER in upper:
            saw_vib_section = True
            last_vib_section_negative_count = 0
            continue

        neg_count = sum(1 for _ in NEG_FREQ_RE.finditer(line))
        total_negative_count += neg_count
        if saw_vib_section:
            last_vib_section_negative_count += neg_count

    if saw_vib_section:
        return last_vib_section_negative_count, irc_found
    return total_negative_count, irc_found


def _scan_ts_full_for_imag_count(out_path: Path, encoding: str) -> tuple[int, bool]:
    with out_path.open("r", encoding=encoding, errors="ignore") as handle:
        return _scan_ts_lines_for_imag_count(handle)


def _scan_ts_text_for_imag_count(text: str) -> tuple[int, bool]:
    return _scan_ts_lines_for_imag_count(text.splitlines())


def analyze_output(out_path: Path, mode: CompletionMode) -> OutAnalysis:
    markers = _default_markers(out_path)
    logger.debug("Analyzing output: %s (mode=%s)", out_path, mode.kind)
    if not out_path.exists():
        return OutAnalysis(
            status=AnalyzerStatus.INCOMPLETE, reason="output_missing", markers=markers
        )

    try:
        encoding = "utf-8"
        file_size = out_path.stat().st_size
        tail_bytes = _TS_TAIL_BYTES if mode.kind == "ts" else _DEFAULT_TAIL_BYTES
        full_text: str | None = None

        if file_size <= tail_bytes:
            # Small file: read entirely (identical to previous behaviour).
            with out_path.open("r", encoding=encoding, errors="ignore") as handle:
                full_text = handle.read()
            _scan_text_for_markers(full_text, markers)
        else:
            # Large file: tail-first strategy.
            tail_text = _read_tail(out_path, encoding, tail_bytes)
            _scan_text_for_markers(tail_text, markers)

            # Head scan: multiplicity_impossible typically appears near the top.
            if not markers["multiplicity_impossible"]:
                head_text = _read_head(out_path, encoding, _HEAD_BYTES)
                head_markers = _default_markers(out_path)
                _scan_text_for_markers(head_text, head_markers)
                if head_markers["multiplicity_impossible"]:
                    markers["multiplicity_impossible"] = True

        # TS mode needs exact imaginary frequency count from the final vibration block.
        if mode.kind == "ts" and markers["terminated_normally"]:
            if full_text is None:
                imag_count, irc_found = _scan_ts_full_for_imag_count(out_path, encoding)
            else:
                imag_count, irc_found = _scan_ts_text_for_imag_count(full_text)
            markers["imaginary_frequency_count"] = imag_count
            if irc_found:
                markers["irc_marker_found"] = True

    except OSError:
        return OutAnalysis(
            status=AnalyzerStatus.INCOMPLETE, reason="output_read_error", markers=markers
        )

    return _interpret_markers(markers, mode)
