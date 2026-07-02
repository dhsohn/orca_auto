"""DFT calculation result index — manages structured metadata in a separate SQLite DB (dft.db).

Parses ORCA output files, stores them in the dft_calculations table,
and performs SQL searches with various filter conditions.
"""

from __future__ import annotations

import logging
from typing import Any

from ..parser import parse_orca_output
from .index_queries import (
    build_dft_query,
    comparison_filters,
    lowest_energy_filters,
)
from .index_scanner import DFTIndexScanner, normalize_status_override
from .index_store import DFTIndexStore

logger = logging.getLogger(__name__)


class DFTIndex:
    """Manages a structured index of DFT calculation results."""

    def __init__(self) -> None:
        self._store = DFTIndexStore()
        self._scanner = DFTIndexScanner()

    def initialize(self, db_path: str) -> None:
        """Open the database and create the schema."""
        self._store.initialize(db_path)
        logger.info("dft_index_initialized: db_path=%s", db_path)

    def close(self) -> None:
        """Close the database connection."""
        self._store.close()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_calculations(
        self,
        kb_dirs: list[str],
        *,
        max_file_size_mb: int = 64,
    ) -> dict[str, Any]:
        """Scan and index ORCA output files from kb_dirs.

        Incremental indexing based on file_hash: only re-parses changed files.

        Returns:
            {"indexed": int, "skipped": int, "removed": int, "failed": int, "total": int}
        """
        existing = self._store.existing_signatures()
        max_bytes = max_file_size_mb * 1024 * 1024
        discovered = self._scanner.discover_targets(kb_dirs, max_bytes=max_bytes)
        to_index, to_remove = self._scanner.changed_targets(existing, discovered)

        indexed = 0
        failed = 0
        results: list[Any] = []
        for source_path, (_, status_override) in to_index.items():
            try:
                result = parse_orca_output(source_path)
                if status_override is not None:
                    result.status = status_override
                results.append(result)
                indexed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("dft_parse_failed: path=%s error=%s", source_path, exc)
                failed += 1

        removed = self._store.apply_index_changes(remove_paths=to_remove, results=results)
        total = self._store.count()
        logger.info(
            "dft_index_complete: indexed=%d skipped=%d removed=%d failed=%d total=%d",
            indexed,
            len(discovered) - len(to_index),
            removed,
            failed,
            total,
        )
        return {
            "indexed": indexed,
            "skipped": len(discovered) - len(to_index),
            "removed": removed,
            "failed": failed,
            "total": total,
        }

    def upsert_single(self, file_path: str, *, status_override: str | None = None) -> bool:
        """Parse and upsert a single file. Returns True on success."""
        try:
            result = parse_orca_output(file_path)
            normalized_override = normalize_status_override(status_override)
            if normalized_override is not None:
                result.status = normalized_override
            self._store.upsert_result(result)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("dft_upsert_failed: path=%s error=%s", file_path, exc)
            return False

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def query(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """Search calculation results with dynamic filter conditions.

        Supported filters:
            method, basis_set, calc_type, status, formula,
            energy_min, energy_max, opt_converged, has_imaginary_freq
        """
        sql, params = build_dft_query(filters)
        return self._store.query(sql, params)

    def get_stats(self) -> dict[str, Any]:
        """Return overall index statistics."""
        return self._store.stats()

    def get_lowest_energy(
        self,
        formula: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the calculation results with the lowest energy."""
        return self.query(lowest_energy_filters(formula, limit))

    def search_by_formula(self, formula: str) -> list[dict[str, Any]]:
        """Search by chemical formula (exact match + LIKE)."""
        exact = self.query({"formula": formula})
        if exact:
            return exact
        return self.query({"formula_like": formula})

    def get_for_comparison(
        self,
        formula: str | None = None,
        method: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return data for comparative analysis, sorted by energy."""
        return self.query(comparison_filters(formula=formula, method=method))
