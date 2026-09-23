"""Read-only ORCA structure evidence; independent of report rendering and publication."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from .frequencies import FrequencyAnalysis, parse_frequency_analysis_text
from .input_blocks import file_route_lines
from .orca_opt_progress import OptProgress, parse_opt_progress_text
from .parser import OrcaResult, parse_orca_output_text
from .parser.io import read_orca_text
from .relaxed_scan import first_scan_coordinate_spec

# Route families whose final geometry is not a stationary point: path methods
# (plain NEB / NEB-CI — NEB-TS is claimed by the TS check first) and dynamics.
# Their endpoints must never be published as structures in an SI. No SCAN
# token here: `SCAN` in a route line is the density functional
# (`! SCAN def2-SVP Opt Freq`), not a scan job — relaxed scans are identified
# from the `%geom Scan` block and OptTS by the TS check.
_NON_STATIONARY_ROUTE_RE = re.compile(
    r"\b(?:ZOOM-)?NEB(?:-CI)?\b|\bMD\b",
    re.IGNORECASE,
)


class OrcaEvidenceError(Exception):
    """The job is missing its final output, energy, or geometry."""


def final_out_path(state: Mapping[str, Any]) -> Path | None:
    """Output file of the run's recorded final result.

    A recorded final ``last_out_path`` is authoritative: when it is absent on
    disk the run has no trustworthy output, and an earlier attempt's numbers
    must never stand in for it. The attempt scan remains only for records
    that never captured a final result path.
    """
    final_result = state.get("final_result")
    if isinstance(final_result, Mapping):
        last_out = str(final_result.get("last_out_path") or "").strip()
        if last_out:
            path = Path(last_out)
            return path if path.exists() else None
    attempts = state.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    for attempt in reversed(attempts):
        if not isinstance(attempt, Mapping):
            continue
        out_raw = str(attempt.get("out_path") or "").strip()
        if out_raw and Path(out_raw).exists():
            return Path(out_raw)
    return None


def structure_kind(selected_inp: Path) -> str | None:
    """``"ts"`` / ``"min"`` / ``"sp"``; ``None`` for non-stationary jobs.

    A plain relaxed scan (Opt route + scan coordinate), IRC, plain NEB paths,
    and MD end on non-stationary geometries that must never enter the
    stationary-structure SI path; IRC has a separate summary-only writer. TS
    routes (OptTS/NEB-TS) and plain Opt end on stationary points.
    Everything else (single points, bare Freq) is reported without a minimum/TS
    claim.
    """
    routes = " ".join(file_route_lines(selected_inp))
    if not routes:
        return None
    if IRC_ROUTE_RE.search(routes):
        return None
    if TS_ROUTE_RE.search(routes):
        return "ts"
    if _NON_STATIONARY_ROUTE_RE.search(routes):
        return None
    if OPT_ROUTE_RE.search(routes):
        if first_scan_coordinate_spec(selected_inp) is not None:
            return None
        return "min"
    return "sp"


@dataclass(frozen=True)
class _ParsedOutput:
    final_output: tuple[OrcaResult, FrequencyAnalysis | None]
    optimization: OptProgress


@lru_cache(maxsize=32)
def _parsed_output_cached(out_path_text: str, mtime_ns: int, size: int) -> _ParsedOutput:
    text = read_orca_text(out_path_text)
    return _ParsedOutput(
        final_output=(
            parse_orca_output_text(text, source_path=out_path_text),
            parse_frequency_analysis_text(text),
        ),
        optimization=parse_opt_progress_text(text, source_path=out_path_text),
    )


def parsed_final_output(out_path: Path) -> tuple[OrcaResult, FrequencyAnalysis | None]:
    """Parsed (result, frequency analysis), cached per (path, mtime, size).

    Report sections reuse the same final output facts. Callers must treat both
    returned objects as read-only because they are shared across cache hits.
    """
    return _parsed_output(out_path).final_output


def _parsed_output(out_path: Path) -> _ParsedOutput:
    stat = out_path.stat()
    return _parsed_output_cached(str(out_path), stat.st_mtime_ns, stat.st_size)


def parsed_optimization_progress(out_path: Path) -> OptProgress:
    """Read-only progress from the same decoded snapshot as final evidence.

    The bounded cache retains parsed facts, never the full output text.
    """
    return _parsed_output(out_path).optimization


def parsed_frequency_analysis(out_path: Path) -> FrequencyAnalysis | None:
    """Reuse even an absent frequency section; read failures remain retryable."""
    return parsed_final_output(out_path)[1]


@dataclass(frozen=True)
class OrcaStructureEvidence:
    """Final structure facts shared by job reports and SI blocks."""

    name: str
    kind: str
    result: OrcaResult
    analysis: FrequencyAnalysis | None
    imaginary_count: int | None
    last_out_name: str = ""
    provenance_warnings: tuple[str, ...] = ()


def collect_structure_evidence(
    reaction_dir: Path, state: Mapping[str, Any]
) -> OrcaStructureEvidence | None:
    """Structure evidence for a completed job; ``None`` for non-stationary jobs.

    Raises:
        OrcaEvidenceError: for a job that should have structure evidence but is missing its
            output, final energy, or coordinates.
    """
    if str(state.get("status") or "") != "completed":
        return None
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    # An unreadable input is an error, not "this job type has no structure evidence":
    # every valid ORCA input has at least one route line, so an empty read
    # means the file is gone (archived / moved stage dir).
    if not file_route_lines(selected_inp):
        raise OrcaEvidenceError(f"cannot read route lines from input {selected_inp}")
    kind = structure_kind(selected_inp)
    if kind is None:
        return None

    out_path = final_out_path(state)
    if out_path is None:
        raise OrcaEvidenceError(f"no output file found for {reaction_dir}")
    result, analysis = parsed_final_output(out_path)
    if result.energy_hartree is None or not result.coordinates:
        raise OrcaEvidenceError(f"output {out_path} lacks a final energy or geometry")

    imaginary_count = analysis.imaginary_count() if analysis is not None else None
    return OrcaStructureEvidence(
        name=reaction_dir.name,
        kind=kind,
        result=result,
        analysis=analysis,
        imaginary_count=imaginary_count,
        last_out_name=out_path.name,
    )
