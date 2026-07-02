"""File IO helpers for ORCA parser internals."""

from __future__ import annotations


def read_orca_text(file_path: str) -> str:
    """Read an ORCA output file with automatic encoding detection."""
    with open(file_path, "rb") as f:
        raw = f.read()

    if not raw:
        return ""

    # Use BOM if present
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")

    # UTF-16LE/BE (without BOM) heuristic: high null byte ratio
    nul_ratio = raw.count(0) / len(raw)
    if nul_ratio > 0.20:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue

    # Default UTF-8, fallback with replacement
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")
