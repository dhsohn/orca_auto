"""DFT calculation file change detection and automatic indexing.

Periodically scans kb_dirs to detect newly completed/changed ORCA calculations
and registers them in the DFT index.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..parser import parse_orca_output
from .discovery import discover_orca_targets
from .index import DFTIndex

logger = logging.getLogger(__name__)

FileSignature = tuple[float, int | None, str]
TargetSignature = tuple[str, str, str, FileSignature]


@dataclass
class MonitorResult:
    """Calculation result detected in a single scan."""

    formula: str = ""
    method_basis: str = ""
    energy: str = ""
    status: str = ""
    calc_type: str = ""
    path: str = ""
    note: str = ""


@dataclass
class ParseFailure:
    """Record of a file that failed to parse."""

    path: str
    error: str
    error_type: str


@dataclass
class ScanReport:
    """Scan result summary."""

    new_results: list[MonitorResult] = field(default_factory=list)
    failures: list[ParseFailure] = field(default_factory=list)
    scanned_files: int = 0
    baseline_seeded: bool = False


@dataclass
class _ScanAccumulator:
    new_results: list[MonitorResult] = field(default_factory=list)
    failures: list[ParseFailure] = field(default_factory=list)
    scanned_signatures: dict[str, FileSignature] = field(default_factory=dict)
    processed_this_scan: set[str] = field(default_factory=set)
    state_dirty: bool = False

    def report(self, *, baseline_seeded: bool = False) -> ScanReport:
        return ScanReport(
            new_results=self.new_results,
            failures=self.failures,
            scanned_files=len(self.scanned_signatures),
            baseline_seeded=baseline_seeded,
        )


class DFTMonitor:
    """DFT calculation file change detection and automatic indexing."""

    def __init__(
        self,
        dft_index: DFTIndex,
        kb_dirs: list[str],
        *,
        state_file: str | None = None,
    ) -> None:
        self._index = dft_index
        self._kb_dirs = kb_dirs
        self._state_file = state_file
        self._last_seen: dict[str, FileSignature] = _load_state(state_file) if state_file else {}
        self._baseline_seeded = bool(self._last_seen)

    def scan(
        self,
        *,
        max_file_size_mb: int = 64,
    ) -> ScanReport:
        """Detect new/changed ORCA files in kb_dirs and index them."""
        max_bytes = max_file_size_mb * 1024 * 1024
        scan_state = _ScanAccumulator()

        for target_info in _iter_target_signatures(
            self._kb_dirs,
            max_bytes=max_bytes,
        ):
            self._scan_target(target_info, scan_state)

        if not self._baseline_seeded:
            return self._seed_baseline(scan_state)

        stale_paths = self._remove_stale_signatures(scan_state.scanned_signatures)
        state_dirty = scan_state.state_dirty or bool(stale_paths)

        if state_dirty and self._state_file:
            _save_state(self._state_file, self._last_seen)

        logger.info(
            "dft_monitor_scan_complete: scanned=%d new=%d stale_removed=%d",
            len(scan_state.scanned_signatures),
            len(scan_state.new_results),
            len(stale_paths),
        )

        return scan_state.report()

    def _scan_target(self, target_info: TargetSignature, scan_state: _ScanAccumulator) -> None:
        spath, canonical, status_override, signature = target_info
        scan_state.scanned_signatures[canonical] = signature

        if not self._should_process(canonical, signature, scan_state.processed_this_scan):
            return
        scan_state.processed_this_scan.add(canonical)

        try:
            result = parse_orca_output(spath)
            effective_status = status_override or result.status
            if not self._record_scan_result(spath, canonical, signature, effective_status):
                return
            scan_state.state_dirty = True
            scan_state.new_results.append(
                _monitor_result_from_orca(result, effective_status, canonical)
            )
            logger.info(
                "dft_monitor_new_calc: path=%s formula=%s method=%s status=%s",
                spath,
                result.formula,
                result.method,
                effective_status,
            )
        except Exception as exc:  # noqa: BLE001
            scan_state.failures.append(_parse_failure(canonical, exc))
            logger.warning("dft_monitor_parse_error: path=%s error=%s", spath, exc)

    def _seed_baseline(self, scan_state: _ScanAccumulator) -> ScanReport:
        self._last_seen.clear()
        self._last_seen.update(scan_state.scanned_signatures)
        self._baseline_seeded = True
        if self._state_file:
            _save_state(self._state_file, self._last_seen)
        logger.info("dft_monitor_baseline_seeded: file_count=%d", len(self._last_seen))
        return scan_state.report(baseline_seeded=True)

    def _remove_stale_signatures(
        self,
        scanned_signatures: dict[str, FileSignature],
    ) -> set[str]:
        stale_paths = set(self._last_seen) - set(scanned_signatures)
        for stale in stale_paths:
            self._last_seen.pop(stale, None)
        return stale_paths

    def _should_process(
        self,
        canonical: str,
        signature: FileSignature,
        processed_this_scan: set[str],
    ) -> bool:
        if not self._baseline_seeded:
            return False
        last_signature = self._last_seen.get(canonical)
        if last_signature is not None and _same_signature(last_signature, signature):
            return False
        return canonical not in processed_this_scan

    def _record_scan_result(
        self,
        spath: str,
        canonical: str,
        signature: FileSignature,
        effective_status: str,
    ) -> bool:
        if effective_status != "running":
            success = self._index.upsert_single(spath, status_override=effective_status)
            if not success:
                return False
        self._last_seen[canonical] = signature
        return True


def _iter_target_signatures(
    kb_dirs: list[str],
    *,
    max_bytes: int,
) -> Iterator[TargetSignature]:
    for kb_dir in kb_dirs:
        kb_path = Path(kb_dir)
        if not kb_path.is_dir():
            logger.warning("dft_monitor_kb_dir_missing: %s", kb_dir)
            continue
        for target in discover_orca_targets(
            kb_path,
            max_bytes=max_bytes,
        ):
            spath = str(target.path)
            canonical = _canonical_path_key(spath)
            status_override = _run_state_status_override(target.run_state_status)
            signature = _file_signature(spath, state_marker=status_override)
            if signature is not None:
                yield spath, canonical, status_override, signature


def _monitor_result_from_orca(result: Any, effective_status: str, canonical: str) -> MonitorResult:
    energy_str = (
        f"E = {result.energy_hartree:.6f} Eh" if result.energy_hartree is not None else "E = N/A"
    )
    method_basis = result.method
    if result.basis_set:
        method_basis += f"/{result.basis_set}"
    notes = _monitor_notes(result)
    note_str = f" ({', '.join(notes)})" if notes else ""
    return MonitorResult(
        formula=result.formula or "unknown",
        method_basis=method_basis or "unknown",
        energy=energy_str,
        status=effective_status,
        calc_type=result.calc_type,
        path=_short_path(canonical),
        note=note_str,
    )


def _monitor_notes(result: Any) -> list[str]:
    notes: list[str] = []
    if result.opt_converged is False:
        notes.append("NOT CONVERGED")
    if result.has_imaginary_freq:
        notes.append("imaginary freq")
    return notes


def _parse_failure(canonical: str, exc: Exception) -> ParseFailure:
    return ParseFailure(
        path=_short_path(canonical),
        error=str(exc),
        error_type=type(exc).__name__,
    )


def _short_path(path: str) -> str:
    """Abbreviate a long path to its last 3 segments."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= 3:
        return path
    return "/".join(parts[-3:])


def _canonical_path_key(path: str | Path) -> str:
    """Normalize path aliases for the same file into a single canonical key."""
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(Path(path).expanduser().absolute())


def _run_state_status_override(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"created", "pending", "running", "retrying"}:
        return "running"
    if normalized in {"completed", "failed", "cancelled"}:
        return normalized
    return ""


def _file_signature(path: str | Path, *, state_marker: str = "") -> FileSignature | None:
    try:
        stat_result = Path(path).stat()
    except OSError:
        return None
    return (float(stat_result.st_mtime), int(stat_result.st_size), state_marker)


def _same_signature(previous: FileSignature, current: FileSignature) -> bool:
    prev_mtime, prev_size, prev_state = previous
    curr_mtime, curr_size, curr_state = current
    if prev_mtime != curr_mtime:
        return False
    if prev_state != curr_state:
        return False
    return prev_size == curr_size


def _signature_sort_key(signature: FileSignature) -> tuple[float, int, str]:
    mtime, size, state = signature
    return (mtime, -1 if size is None else size, state)


def _load_signature(value: Any) -> FileSignature | None:
    if not isinstance(value, dict):
        return None

    raw_mtime = value.get("mtime")
    if not isinstance(raw_mtime, (int, float, str)):
        return None
    try:
        mtime = float(raw_mtime)
    except (TypeError, ValueError):
        return None

    raw_size = value.get("size")
    raw_state = value.get("state")
    state = raw_state.strip().lower() if isinstance(raw_state, str) else ""
    if raw_size is None:
        return None
    try:
        return (mtime, int(raw_size), state)
    except (TypeError, ValueError):
        return None


def _load_state(state_file: str | None) -> dict[str, FileSignature]:
    """Load dft_monitor state from disk."""
    if not state_file:
        return {}
    try:
        with open(state_file, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        state: dict[str, FileSignature] = {}
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            normalized_key = _canonical_path_key(k)
            signature = _load_signature(v)
            if signature is None:
                continue
            previous = state.get(normalized_key)
            if previous is None or _signature_sort_key(signature) > _signature_sort_key(previous):
                state[normalized_key] = signature
        return state
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("dft_monitor_state_load_failed: path=%s error=%s", state_file, exc)
        return {}


def _save_state(state_file: str | None, signatures: dict[str, FileSignature]) -> None:
    """Atomically save dft_monitor state."""
    if not state_file:
        return
    try:
        path = Path(state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        payload = {
            key: {"mtime": mtime, "size": size, "state": state}
            for key, (mtime, size, state) in signatures.items()
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp_path.replace(path)
    except OSError as exc:
        logger.warning("dft_monitor_state_save_failed: path=%s error=%s", state_file, exc)
