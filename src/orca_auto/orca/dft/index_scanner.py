from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .discovery import discover_orca_targets

logger = logging.getLogger(__name__)


def normalize_status_override(status: str | None) -> str | None:
    normalized = str(status or "").strip().lower()
    if normalized in {"created", "pending", "running", "retrying"}:
        return "running"
    if normalized in {"completed", "failed", "cancelled"}:
        return normalized
    return None


def short_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


class DFTIndexScanner:
    def discover_targets(
        self,
        kb_dirs: list[str],
        *,
        max_bytes: int,
    ) -> dict[str, tuple[str, str | None]]:
        discovered: dict[str, tuple[str, str | None]] = {}
        for kb_dir in kb_dirs:
            kb_path = Path(kb_dir)
            if not kb_path.is_dir():
                logger.warning("dft_kb_dir_not_found: path=%s", kb_dir)
                continue
            for target in discover_orca_targets(kb_path, max_bytes=max_bytes):
                discovered[str(target.path)] = (
                    short_file_hash(target.path),
                    normalize_status_override(target.run_state_status),
                )
        return discovered

    def changed_targets(
        self,
        existing: dict[str, tuple[str, str]],
        discovered: dict[str, tuple[str, str | None]],
    ) -> tuple[dict[str, tuple[str, str | None]], set[str]]:
        to_index = {
            path: payload
            for path, payload in discovered.items()
            if existing.get(path)
            != (
                payload[0],
                payload[1] or "",
            )
        }
        to_remove = set(existing) - set(discovered)
        return to_index, to_remove
