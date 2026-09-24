"""Import-safe identity for the one supported engine (standalone ORCA).

The catalog carries only what is persisted as queue/admission identity or read
by the worker at admission time. It imports nothing else from ``orca_auto.orca``
so the CLI and activity code can consult engine identity without pulling in the
execution stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class EngineCatalogEntry:
    """Persisted identity and admission policy for one engine."""

    engine_id: str
    app_id: str
    source_id: str
    task_kinds: tuple[str, ...]
    # Durable identity label written to ``admission_slots.json`` (``source``
    # field) on every reservation. It is NOT a module path; do not change it
    # without migrating persisted admission records.
    admission_source: str
    engine_launch_gated: bool


_ENGINE_CATALOG: Final[tuple[EngineCatalogEntry, ...]] = (
    EngineCatalogEntry(
        engine_id="orca",
        app_id="orca_auto_orca",
        source_id="orca_auto_orca",
        task_kinds=("orca_run_inp",),
        admission_source="orca_auto.orca.queue_worker",
        engine_launch_gated=True,
    ),
)

_ENGINE_BY_ID: Final[dict[str, EngineCatalogEntry]] = {
    entry.engine_id: entry for entry in _ENGINE_CATALOG
}

if len(_ENGINE_BY_ID) != len(_ENGINE_CATALOG):
    raise RuntimeError("duplicate engine id in built-in engine catalog")
if len({entry.app_id for entry in _ENGINE_CATALOG}) != len(_ENGINE_CATALOG):
    raise RuntimeError("duplicate app id in built-in engine catalog")
if len({entry.source_id for entry in _ENGINE_CATALOG}) != len(_ENGINE_CATALOG):
    raise RuntimeError("duplicate source id in built-in engine catalog")


def engine_catalog() -> tuple[EngineCatalogEntry, ...]:
    return _ENGINE_CATALOG


def find_engine_catalog_entry(engine: object) -> EngineCatalogEntry | None:
    engine_id = str(engine or "").strip().lower().replace("-", "_")
    return _ENGINE_BY_ID.get(engine_id)


def get_engine_catalog_entry(engine: object) -> EngineCatalogEntry:
    entry = find_engine_catalog_entry(engine)
    if entry is not None:
        return entry
    engine_id = str(engine or "").strip().lower().replace("-", "_")
    supported = ", ".join(entry.engine_id for entry in _ENGINE_CATALOG)
    raise ValueError(f"unsupported engine: {engine_id or '<blank>'} (supported: {supported})")


__all__ = [
    "EngineCatalogEntry",
    "engine_catalog",
    "find_engine_catalog_entry",
    "get_engine_catalog_entry",
]
