from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orca_auto.core.engine_catalog import find_engine_catalog_entry


def _entry_text(entry: Any, field: str) -> str:
    return str(getattr(entry, field, "") or "").strip()


def entry_matches_engine_identity(entry: Any, engine: str) -> bool:
    """Return whether a queue row has the complete canonical engine identity."""
    expected_engine = str(engine).strip()
    catalog_entry = find_engine_catalog_entry(expected_engine)
    task_kind = _entry_text(entry, "task_kind")
    if catalog_entry is None:
        expected_app_name = f"orca_auto_{expected_engine}"
        task_kind_matches = task_kind.startswith(f"{expected_engine}_") and bool(
            task_kind.removeprefix(f"{expected_engine}_")
        )
    else:
        expected_app_name = catalog_entry.app_id
        task_kind_matches = task_kind in catalog_entry.task_kinds
    if expected_engine == "xtb" and catalog_entry is not None:
        metadata = getattr(entry, "metadata", {})
        job_type = str(metadata.get("job_type") or "").strip() if isinstance(metadata, dict) else ""
        task_kind_matches = task_kind_matches and (
            task_kind == f"xtb_{job_type}" and task_kind in catalog_entry.task_kinds
        )
    return bool(
        expected_engine
        and _entry_text(entry, "app_name") == expected_app_name
        and _entry_text(entry, "engine") == expected_engine
        and _entry_text(entry, "queue_id")
        and _entry_text(entry, "task_id")
        and task_kind_matches
    )


def own_engine_accept_entry(engine: str) -> Callable[[Any], bool]:
    """Build a predicate that claims only rows with a complete engine identity.

    Engine workers share runtime roots, so incomplete, conflicting, and
    historical labels remain durable for inspection but are quarantined from
    execution.
    """
    return lambda entry: entry_matches_engine_identity(entry, engine)


__all__ = ["entry_matches_engine_identity", "own_engine_accept_entry"]
