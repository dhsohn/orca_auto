"""Read confined ORCA energy evidence for workflow reports."""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import read_confined_text
from orca_auto.core.utils import mapping_or_empty as _mapping
from orca_auto.orca.parser.patterns import (
    FINAL_SINGLE_POINT_ENERGY_BYTES_RE,
    final_single_point_energy_value,
)

from . import report_diagnostics

_ENGRAD_ENERGY_MARKER = "current total energy"
_MAX_ENGRAD_ENERGY_FILE_BYTES = 8 * 1024 * 1024
_ORCA_ENERGY_SCAN_WINDOW_BYTES = 256 * 1024
# Consecutive scan windows overlap by this much so a line cut at a window's
# start is seen whole by the next window; it must exceed the longest possible
# final-energy line (marker + value + annotation, well under 200 bytes).
_ORCA_ENERGY_SCAN_OVERLAP_BYTES = 4 * 1024
# A window strictly larger than the overlap is what makes each backward step
# progress; equality would rescan the same window forever.
assert _ORCA_ENERGY_SCAN_WINDOW_BYTES > _ORCA_ENERGY_SCAN_OVERLAP_BYTES
_MAX_ORCA_ENERGY_CANDIDATES = 8
_ORCA_ENERGY_READ_CHUNK_BYTES = 64 * 1024


def latest_engrad_energy(directory: Path) -> float | None:
    """Total energy (Eh) from the most recent ``*.engrad`` in ``directory``."""
    try:
        resolved_directory = directory.expanduser().resolve(strict=True)
        directory_details = directory.lstat()
        if directory != resolved_directory or not stat.S_ISDIR(directory_details.st_mode):
            return None
        candidates: list[tuple[int, Path]] = []
        for entry in directory.glob("*.engrad"):
            details = entry.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(details.st_mode)
                and details.st_nlink == 1
                and details.st_size <= _MAX_ENGRAD_ENERGY_FILE_BYTES
            ):
                candidates.append((int(details.st_mtime_ns), entry))
        candidates.sort(key=lambda item: item[0], reverse=True)
    except (OSError, RuntimeError):
        return None
    for _mtime_ns, candidate in candidates:
        try:
            lines = read_confined_text(
                resolved_directory,
                candidate,
                label="ORCA gradient energy",
                max_bytes=_MAX_ENGRAD_ENERGY_FILE_BYTES,
            ).splitlines()
        except (OSError, RuntimeError, ValueError):
            continue
        marker_seen = False
        for line in lines:
            stripped = line.strip()
            if _ENGRAD_ENERGY_MARKER in stripped.lower():
                marker_seen = True
                continue
            if not marker_seen or not stripped or stripped.startswith("#"):
                continue
            try:
                parsed = float(stripped)
            except ValueError:
                break
            # A corrupt .engrad can spell nan/inf; a non-finite energy must
            # not reach the report or the machine observation.
            return parsed if math.isfinite(parsed) else None
    return None


def _pread_exact(descriptor: int, offset: int, size: int) -> bytes | None:
    """Read exactly ``size`` bytes at ``offset``, or None on a short read."""
    buffer = bytearray()
    while len(buffer) < size:
        chunk = os.pread(
            descriptor,
            min(_ORCA_ENERGY_READ_CHUNK_BYTES, size - len(buffer)),
            offset + len(buffer),
        )
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) != size:
        return None
    return bytes(buffer)


def _last_final_energy_line_from_output(
    output_root: Path,
    candidate: Path,
) -> tuple[bool, bytes | None] | None:
    """Locate the file-final energy line of one confined, stable output.

    Scans fixed-size windows backwards from EOF until the newest complete
    ``FINAL SINGLE POINT ENERGY`` line is found, so the read cost is bounded
    by that line's distance from EOF rather than by a fixed tail; only an
    output that prints no final-energy line at all is read in full. Freq
    blocks routinely push that line several hundred KiB before EOF, which a
    single bounded tail cannot see past.

    Returns ``None`` when the output cannot be read safely, and
    ``(annotated, energy_text)`` otherwise; ``energy_text`` is
    ``None`` when no final-energy line exists or the last one is annotated.
    """
    parent_fd = -1
    output_fd = -1
    try:
        root = output_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
        if not relative.parts:
            return None

        candidate_before = os.stat(candidate, follow_symlinks=False)
        if not stat.S_ISREG(candidate_before.st_mode) or candidate_before.st_nlink != 1:
            return None

        # The parent is opened by its already-resolved path. O_NOFOLLOW rejects a
        # symlink only at the final component, so this does NOT prove by
        # construction that the parent is still inside the root — a walk from the
        # root would. That stronger guarantee is traded away deliberately: a
        # concurrent path swap by another process on this account is outside this
        # tool's declared operating model, and the inode comparison below covers
        # every swap that happens after the stat.
        directory_flags = os.O_RDONLY | os.O_CLOEXEC
        directory_flags |= os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(resolved.parent, directory_flags)

        output_flags = os.O_RDONLY | os.O_CLOEXEC
        # O_NONBLOCK covers the stat-to-open race: without it a path swapped to a
        # FIFO between the two would block the report writer indefinitely.
        output_flags |= os.O_NONBLOCK | os.O_NOFOLLOW
        output_fd = os.open(relative.parts[-1], output_flags, dir_fd=parent_fd)
        opened = os.fstat(output_fd)
        # The fd pins one inode. Comparing it against the pre-open stat rejects
        # anything swapped between that stat and this open. A swap that already
        # happened before the stat is not covered: both sides would then observe
        # the substituted file and agree.
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (candidate_before.st_dev, candidate_before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return None

        found: re.Match[bytes] | None = None
        window_end = opened.st_size
        while True:
            window_start = max(0, window_end - _ORCA_ENERGY_SCAN_WINDOW_BYTES)
            window = _pread_exact(output_fd, window_start, window_end - window_start)
            if window is None:
                return None
            for match in FINAL_SINGLE_POINT_ENERGY_BYTES_RE.finditer(window):
                # A window can start in the middle of a line. Do not treat that
                # truncated first line as a complete ORCA marker; the next
                # window's overlap re-reads it with its true line start.
                if window_start and match.start() == 0:
                    continue
                found = match
            if found is not None or window_start == 0:
                break
            window_end = window_start + _ORCA_ENERGY_SCAN_OVERLAP_BYTES

        after = os.fstat(output_fd)
        # A file that changed under the scan must be rejected, not parsed. The
        # energy marker's `$` also matches at the end of a window buffer, so a
        # half-written number would parse as a complete value and be displayed
        # as a wrong energy in the delta-E table.
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        if found is None:
            return False, None
        if found.group(2) is not None:
            return True, None
        return False, found.group(1)
    except (OSError, ValueError):
        return None
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _final_single_point_energy_from_output(
    output_root: Path, candidate: Path
) -> tuple[bool, float | None]:
    """Return ``(final_line_annotated, energy)`` for one confined output."""
    scan = _last_final_energy_line_from_output(output_root, candidate)
    if scan is None:
        return False, None
    annotated, energy_text = scan
    if annotated:
        # The final energy line is annotated ("(SCF not fully converged!)"):
        # an unconverged value must not feed the ΔE table, and any earlier
        # clean line belongs to a different geometry — forget it rather than
        # falling back to it.
        return True, None
    if energy_text is None:
        return False, None
    try:
        energy = final_single_point_energy_value(energy_text)
    except ValueError:
        return False, None
    return False, energy if math.isfinite(energy) else None


def orca_report_output_energy_state(
    output_dir: Path,
    report_payload: Mapping[str, Any],
) -> tuple[bool, float | None]:
    """Return ``(final_line_annotated, energy)`` for the stage's output chain."""
    try:
        output_root = output_dir.resolve(strict=True)
    except OSError:
        return False, None

    engine_payload = _mapping(report_payload.get("engine_payload"))
    final_result = _mapping(engine_payload.get("final_result"))
    final_out_path = report_diagnostics.normalized_text(final_result.get("last_out_path"))
    candidates = [final_out_path]
    attempts = engine_payload.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts[-(_MAX_ORCA_ENERGY_CANDIDATES - 1) :]):
            if isinstance(attempt, Mapping):
                candidates.append(report_diagnostics.normalized_text(attempt.get("out_path")))

    seen: set[str] = set()
    for position, raw_path in enumerate(candidates):
        if not raw_path:
            continue
        candidate_key = os.path.abspath(raw_path)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        annotated, energy = _final_single_point_energy_from_output(output_root, Path(raw_path))
        if annotated:
            # The newest readable output in the chain is tainted by an
            # unconverged SCF; older attempts must not stand in for it.
            return True, None
        if energy is not None:
            if final_out_path and position > 0:
                # A recorded final output is authoritative: when it is
                # missing, unreadable, or prints no final energy line, an
                # earlier attempt's clean value belongs to a different
                # geometry and must not stand in for it. Older attempts stay
                # consulted above as annotation evidence only — mirrors the
                # per-job final_out_path rule.
                return False, None
            return False, energy
    return False, None


__all__ = [
    "latest_engrad_energy",
    "orca_report_output_energy_state",
]
