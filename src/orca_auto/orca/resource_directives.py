from __future__ import annotations

import math
import re
from pathlib import Path

from orca_auto.core.engine_process import atomic_write_confined_bytes

from .input_blocks import (
    BLOCK_START_RE,
    GEOM_HEADER_RE,
    active_orca_directive_text,
    find_route_idx,
    orca_route_line,
    set_block_key_value,
)

MAXCORE_RE = re.compile(r"^\s*%maxcore\s+(\d+)", re.IGNORECASE)
NPROCS_RE = re.compile(r"\bnprocs\s+(\d+)\b", re.IGNORECASE)
# ORCA route-line shorthand "! PALn" (PAL2..PAL8) requests n parallel processes.
PAL_ROUTE_RE = re.compile(r"\bPAL(\d+)\b", re.IGNORECASE)


def read_maxcore(lines: list[str]) -> int | None:
    values: list[int] = []
    for line in lines:
        active_line = active_orca_directive_text(line)
        m = MAXCORE_RE.match(active_line)
        if m:
            try:
                values.append(int(m.group(1)))
            except ValueError:
                continue
    return max(values) if values else None


def read_nprocs(lines: list[str]) -> int | None:
    # An explicit %pal block wins over the route-line shorthand; fall back to
    # "! PALn" so a PAL-only input is not treated as having no nprocs (which would
    # inject a conflicting %pal block and compute the wrong resource_request).
    values = [
        value
        for value in (
            _read_nprocs_from_pal_block(lines),
            _read_nprocs_from_pal_shorthand(lines),
        )
        if value is not None
    ]
    return max(values) if values else None


def _read_nprocs_from_pal_shorthand(lines: list[str]) -> int | None:
    values: list[int] = []
    for line in lines:
        route = orca_route_line(line)
        if route is None:
            continue
        for match in PAL_ROUTE_RE.finditer(route):
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if value > 0:
                values.append(value)
    return max(values) if values else None


def _read_nprocs_from_pal_block(lines: list[str]) -> int | None:
    in_pal_block = False
    values: list[int] = []
    for line in lines:
        active_line = active_orca_directive_text(line)
        block_match = BLOCK_START_RE.match(active_line)
        if block_match is not None:
            in_pal_block = _is_block_start(block_match, "pal")
            if not in_pal_block:
                continue
            remainder = active_line[block_match.end() :] if block_match else ""
            inline_value = read_nprocs_from_text(remainder)
            if inline_value is not None:
                values.append(inline_value)
            if re.search(r"\bend\b", remainder, re.IGNORECASE):
                in_pal_block = False
            continue

        if not in_pal_block:
            continue

        if ends_pal_block(active_line):
            in_pal_block = False
            continue

        value = read_nprocs_from_text(active_line)
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _is_block_start(block_match: re.Match[str] | None, name: str) -> bool:
    return bool(block_match and block_match.group(1).lower() == name)


def read_nprocs_from_text(text: str) -> int | None:
    values: list[int] = []
    for match in NPROCS_RE.finditer(text):
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return max(values) if values else None


def ends_pal_block(line: str) -> bool:
    stripped = line.strip()
    if stripped.lower() == "end":
        return True
    if BLOCK_START_RE.match(line):
        return True
    return bool(GEOM_HEADER_RE.match(stripped))


def maxcore_mb_per_core(*, max_memory_gb: int, max_cores: int) -> int:
    total_mb = max(1, int(max_memory_gb)) * 1024
    return max(1, total_mb // max(1, int(max_cores)))


def resource_request_from_lines(lines: list[str]) -> dict[str, int]:
    max_cores = read_nprocs(lines)
    maxcore_mb = read_maxcore(lines)
    if max_cores is None or maxcore_mb is None or maxcore_mb <= 0:
        return {}
    total_memory_gb = max(1, math.ceil((max_cores * maxcore_mb) / 1024))
    return {
        "max_cores": max_cores,
        "max_memory_gb": total_memory_gb,
    }


def read_resource_request_from_input(inp_path: Path) -> dict[str, int]:
    lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return resource_request_from_lines(lines)


def ensure_submission_resource_request(
    inp_path: Path,
    *,
    default_max_cores: int,
    default_max_memory_gb: int,
) -> tuple[dict[str, int], list[str]]:
    lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: list[str] = []

    max_cores = read_nprocs(lines)
    if max_cores is None:
        configured_cores = max(1, int(default_max_cores))
        if set_block_key_value(lines, "pal", "nprocs", str(configured_cores)):
            actions.append("pal_nprocs_injected")
        max_cores = read_nprocs(lines) or configured_cores

    maxcore_mb = read_maxcore(lines)
    if maxcore_mb is None or maxcore_mb <= 0:
        configured_maxcore = maxcore_mb_per_core(
            max_memory_gb=max(1, int(default_max_memory_gb)),
            max_cores=max_cores,
        )
        if set_maxcore(lines, configured_maxcore):
            actions.append("maxcore_injected")

    resource_request = resource_request_from_lines(lines)
    if not resource_request:
        raise ValueError(f"Could not determine ORCA resource_request from input: {inp_path}")

    if actions:
        atomic_write_confined_bytes(
            inp_path.parent,
            inp_path,
            ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
            label="ORCA resource-normalized input",
        )
    return resource_request, actions


def set_maxcore(lines: list[str], value_mb: int) -> bool:
    matches = [
        i for i, line in enumerate(lines) if MAXCORE_RE.match(active_orca_directive_text(line))
    ]
    if matches:
        new_line = f"%maxcore {value_mb}"
        first = matches[0]
        changed = lines[first] != new_line or len(matches) > 1
        lines[first] = new_line
        for index in reversed(matches[1:]):
            del lines[index]
        return changed
    insert_at = find_route_idx(lines)
    if insert_at is not None:
        insert_at += 1
    else:
        insert_at = 0
    lines.insert(insert_at, f"%maxcore {value_mb}")
    return True


def clamp_maxcore_to_budget(lines: list[str], *, max_memory_gb: int) -> bool:
    """Cap ``%maxcore`` so per-core memory stays within the per-task budget.

    The per-core ceiling is derived from the input's own ``nprocs`` so
    ``nprocs * maxcore`` does not exceed the configured budget. Returns ``True``
    when the input was changed.
    """
    if max_memory_gb <= 0:
        return False
    current = read_maxcore(lines)
    if current is None:
        return False
    cores = read_nprocs(lines) or 1
    ceiling = maxcore_mb_per_core(max_memory_gb=max_memory_gb, max_cores=cores)
    if current <= ceiling:
        return False
    return set_maxcore(lines, ceiling)
