"""Read-only ORCA structure evidence; independent of report rendering and publication."""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

from .completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from .frequencies import FrequencyAnalysis, parse_frequency_analysis_text
from .input_syntax import file_route_lines
from .orca_opt_progress import OptProgress, parse_opt_progress_text
from .parser import OrcaResult, parse_orca_output_text
from .parser.io import read_orca_text
from .relaxed_scan import first_scan_coordinate_spec
from .statuses import RunStatus

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


def final_out_name(state: Mapping[str, Any]) -> str:
    """Basename of :func:`final_out_path` for report headers; ``""`` without one.

    Report collectors that show a "last output" name should take it from
    here rather than from ``final_result.last_out_path or attempts[-1]``: that
    weaker rule names a file the run no longer has, or an earlier attempt's,
    as if it were the final output.
    """
    path = final_out_path(state)
    return path.name if path is not None else ""


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


T = TypeVar("T")

_PARSED_OUTPUT_CACHE_SIZE = 32


@dataclass(frozen=True, eq=False)
class _ParsedOutput:
    """Facts parsed from one decoded output snapshot; never the text itself.

    ``derived`` memoizes report-specific facts (see :func:`parsed_output_facts`)
    keyed by the parser that produced them, so a job report decodes each
    output once for structure evidence, progress, frequencies, and its own
    path/settings tables alike.
    """

    final_output: tuple[OrcaResult, FrequencyAnalysis | None]
    optimization: OptProgress
    derived: dict[Callable[[str], object], object] = field(default_factory=dict)


class _ParsedOutputCache:
    """Bounded LRU keyed by ``(path, mtime_ns, size)``.

    A hand-rolled LRU rather than ``functools.lru_cache`` because a derived
    parser requested on a cache miss must run on the same decoded text as the
    base facts, which needs a lookup that is separate from the store.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[tuple[str, int, int], _ParsedOutput] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int, int]) -> _ParsedOutput | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, key: tuple[str, int, int], entry: _ParsedOutput) -> _ParsedOutput:
        """Store ``entry`` unless a concurrent reader stored one first; return the kept one."""
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                self._entries.move_to_end(key)
                return existing
            self._entries[key] = entry
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)
            return entry

    def cache_clear(self) -> None:
        with self._lock:
            self._entries.clear()


_parsed_output_cache = _ParsedOutputCache(_PARSED_OUTPUT_CACHE_SIZE)


def _parse_output_text(
    text: str, out_path_text: str, derive: Callable[[str], object] | None
) -> _ParsedOutput:
    snapshot = _ParsedOutput(
        final_output=(
            parse_orca_output_text(text, source_path=out_path_text),
            parse_frequency_analysis_text(text),
        ),
        optimization=parse_opt_progress_text(text, source_path=out_path_text),
    )
    if derive is not None:
        snapshot.derived[derive] = derive(text)
    return snapshot


def parsed_final_output(out_path: Path) -> tuple[OrcaResult, FrequencyAnalysis | None]:
    """Parsed (result, frequency analysis), cached per (path, mtime, size).

    Report sections reuse the same final output facts. Callers must treat both
    returned objects as read-only because they are shared across cache hits.
    """
    return _parsed_output(out_path).final_output


def _parsed_output(out_path: Path, derive: Callable[[str], object] | None = None) -> _ParsedOutput:
    stat = out_path.stat()
    key = (str(out_path), stat.st_mtime_ns, stat.st_size)
    snapshot = _parsed_output_cache.get(key)
    if snapshot is None:
        snapshot = _parsed_output_cache.put(
            key, _parse_output_text(read_orca_text(key[0]), key[0], derive)
        )
    return snapshot


def parsed_optimization_progress(out_path: Path) -> OptProgress:
    """Read-only progress from the same decoded snapshot as final evidence.

    The bounded cache retains parsed facts, never the full output text.
    """
    return _parsed_output(out_path).optimization


def parsed_frequency_analysis(out_path: Path) -> FrequencyAnalysis | None:
    """Reuse even an absent frequency section; read failures remain retryable."""
    return parsed_final_output(out_path)[1]


def parsed_output_facts(out_path: Path, parse_text: Callable[[str], T]) -> T:
    """``parse_text(decoded output)`` memoized on the same snapshot as the base facts.

    For report parsers (IRC/NEB settings, iterations, path summaries) that
    would otherwise decode the output again per attempt. ``parse_text`` must be
    a module-level function: it is the memo key, and a fresh lambda per call
    would defeat the cache. The result is shared across cache hits, so callers
    treat it as read-only. A snapshot cached before this parser was asked for
    decodes the text once more and remembers the result.
    """
    snapshot = _parsed_output(out_path, derive=parse_text)
    try:
        return cast(T, snapshot.derived[parse_text])
    except KeyError:
        facts = parse_text(read_orca_text(str(out_path)))
        return cast(T, snapshot.derived.setdefault(parse_text, facts))


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
    if str(state.get("status") or "") != RunStatus.COMPLETED.value:
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
