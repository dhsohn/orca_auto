from __future__ import annotations

import math
import re
from pathlib import Path

from .input_blocks import (
    BLOCK_START_RE,
    GEOM_HEADER_RE,
    find_route_idx,
    set_block_key_value,
)

MAXCORE_RE = re.compile(r"^\s*%maxcore\s+(\d+)", re.IGNORECASE)
NPROCS_RE = re.compile(r"\bnprocs\s+(\d+)\b", re.IGNORECASE)
DEFAULT_MAXCORE_MB = 4000
MAXCORE_INCREASE_FACTOR = 1.5


def read_maxcore(lines: list[str]) -> int | None:
    for line in lines:
        m = MAXCORE_RE.match(line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def read_nprocs(lines: list[str]) -> int | None:
    in_pal_block = False
    for line in lines:
        block_match = BLOCK_START_RE.match(line)
        if not in_pal_block:
            if not _is_block_start(block_match, "pal"):
                continue
            remainder = line[block_match.end() :] if block_match else ""
            inline_value = read_nprocs_from_text(remainder)
            if inline_value is not None:
                return inline_value
            if re.search(r"\bend\b", remainder, re.IGNORECASE):
                return None
            in_pal_block = True
            continue

        if ends_pal_block(line):
            return None

        value = read_nprocs_from_text(line)
        if value is not None:
            return value
    return None


def _is_block_start(block_match: re.Match[str] | None, name: str) -> bool:
    return bool(block_match and block_match.group(1).lower() == name)


def read_nprocs_from_text(text: str) -> int | None:
    nprocs_match = NPROCS_RE.search(text)
    if not nprocs_match:
        return None
    try:
        value = int(nprocs_match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


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
        inp_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return resource_request, actions


def set_maxcore(lines: list[str], value_mb: int) -> bool:
    for i, line in enumerate(lines):
        m = MAXCORE_RE.match(line)
        if m:
            new_line = f"%maxcore {value_mb}"
            if lines[i].strip() == new_line:
                return False
            lines[i] = new_line
            return True
    insert_at = find_route_idx(lines)
    if insert_at is not None:
        insert_at += 1
    else:
        insert_at = 0
    lines.insert(insert_at, f"%maxcore {value_mb}")
    return True


def increase_maxcore(lines: list[str]) -> bool:
    current = read_maxcore(lines)
    if current is None:
        return set_maxcore(lines, DEFAULT_MAXCORE_MB)
    new_value = int(current * MAXCORE_INCREASE_FACTOR)
    if new_value <= current:
        new_value = current + 1000
    return set_maxcore(lines, new_value)


def clamp_maxcore_to_budget(lines: list[str], *, max_memory_gb: int) -> bool:
    """Cap ``%maxcore`` so per-core memory stays within the per-task budget.

    Retry recipes escalate ``%maxcore`` (see :func:`increase_maxcore`); without
    a ceiling the value can grow past ``max_memory_gb_per_task`` across repeated
    retries. The per-core ceiling is derived from the input's own ``nprocs`` so
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
