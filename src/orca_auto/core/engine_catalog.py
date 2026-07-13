from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

WorkflowStageRole = Literal["shared-root", "workflow-stage"]
SupervisionRole = Literal["default", "with-workflow"]


@dataclass(frozen=True)
class EngineCatalogEntry:
    """Import-safe metadata for a built-in engine.

    Module names are strings on purpose: reading the catalog must not import
    engine implementations from the ORCA or workflow layers.
    """

    engine_id: str
    definition_module: str
    worker_module: str
    admission_source: str
    app_id: str
    source_id: str
    workflow_stage_role: WorkflowStageRole
    workflow_stage_dirname: str
    workflow_stage_aliases: tuple[str, ...]
    managed_admission: bool
    default_supervision_role: SupervisionRole
    supervision_order: int
    activity_order: int
    task_kinds: tuple[str, ...]


_ENGINE_CATALOG: Final[tuple[EngineCatalogEntry, ...]] = (
    EngineCatalogEntry(
        engine_id="orca",
        definition_module="orca_auto.orca.engine",
        worker_module="orca_auto.core.engines.queue_worker",
        admission_source="orca_auto.orca.queue_worker",
        app_id="orca_auto_orca",
        source_id="orca_auto_orca",
        workflow_stage_role="shared-root",
        workflow_stage_dirname="03_orca",
        workflow_stage_aliases=("02_orca",),
        managed_admission=True,
        default_supervision_role="default",
        supervision_order=0,
        activity_order=2,
        task_kinds=("orca_run_inp",),
    ),
    EngineCatalogEntry(
        engine_id="xtb",
        definition_module="orca_auto.flow.engines.xtb.engine",
        worker_module="orca_auto.core.engines.queue_worker",
        admission_source="orca_auto.flow.engines.xtb.queue_worker",
        app_id="orca_auto_xtb",
        source_id="orca_auto_xtb",
        workflow_stage_role="workflow-stage",
        workflow_stage_dirname="02_xtb",
        workflow_stage_aliases=(),
        managed_admission=True,
        default_supervision_role="with-workflow",
        supervision_order=2,
        activity_order=1,
        task_kinds=("xtb_path_search", "xtb_opt", "xtb_sp", "xtb_hess", "xtb_ranking"),
    ),
    EngineCatalogEntry(
        engine_id="crest",
        definition_module="orca_auto.flow.engines.crest.engine",
        worker_module="orca_auto.core.engines.queue_worker",
        admission_source="orca_auto.flow.engines.crest.queue_worker",
        app_id="orca_auto_crest",
        source_id="orca_auto_crest",
        workflow_stage_role="workflow-stage",
        workflow_stage_dirname="01_crest",
        workflow_stage_aliases=(),
        managed_admission=True,
        default_supervision_role="with-workflow",
        supervision_order=1,
        activity_order=0,
        task_kinds=("crest_conformer_search",),
    ),
)

_ENGINE_BY_ID: Final[dict[str, EngineCatalogEntry]] = {
    entry.engine_id: entry for entry in _ENGINE_CATALOG
}
_ENGINE_BY_APP_ID: Final[dict[str, EngineCatalogEntry]] = {
    entry.app_id: entry for entry in _ENGINE_CATALOG
}
_ENGINE_BY_SOURCE_ID: Final[dict[str, EngineCatalogEntry]] = {
    entry.source_id: entry for entry in _ENGINE_CATALOG
}

if len(_ENGINE_BY_ID) != len(_ENGINE_CATALOG):
    raise RuntimeError("duplicate engine id in built-in engine catalog")
if len(_ENGINE_BY_APP_ID) != len(_ENGINE_CATALOG):
    raise RuntimeError("duplicate app id in built-in engine catalog")
if len(_ENGINE_BY_SOURCE_ID) != len(_ENGINE_CATALOG):
    raise RuntimeError("duplicate source id in built-in engine catalog")


def engine_catalog() -> tuple[EngineCatalogEntry, ...]:
    return _ENGINE_CATALOG


def known_engine_ids() -> tuple[str, ...]:
    return tuple(entry.engine_id for entry in _ENGINE_CATALOG)


def find_engine_catalog_entry(engine: object) -> EngineCatalogEntry | None:
    engine_id = str(engine or "").strip().lower().replace("-", "_")
    return _ENGINE_BY_ID.get(engine_id)


def get_engine_catalog_entry(engine: object) -> EngineCatalogEntry:
    entry = find_engine_catalog_entry(engine)
    if entry is not None:
        return entry
    engine_id = str(engine or "").strip().lower().replace("-", "_")
    supported = ", ".join(known_engine_ids())
    raise ValueError(f"unsupported engine: {engine_id or '<blank>'} (supported: {supported})")


def find_engine_catalog_entry_by_app_id(app_id: object) -> EngineCatalogEntry | None:
    return _ENGINE_BY_APP_ID.get(str(app_id or "").strip())


def find_engine_catalog_entry_by_source_id(source_id: object) -> EngineCatalogEntry | None:
    return _ENGINE_BY_SOURCE_ID.get(str(source_id or "").strip())


def workflow_stage_engine_entries() -> tuple[EngineCatalogEntry, ...]:
    return tuple(
        entry for entry in _ENGINE_CATALOG if entry.workflow_stage_role == "workflow-stage"
    )


def supervised_engine_entries() -> tuple[EngineCatalogEntry, ...]:
    return tuple(sorted(_ENGINE_CATALOG, key=lambda entry: entry.supervision_order))


def activity_engine_entries() -> tuple[EngineCatalogEntry, ...]:
    return tuple(sorted(_ENGINE_CATALOG, key=lambda entry: entry.activity_order))


__all__ = [
    "EngineCatalogEntry",
    "SupervisionRole",
    "WorkflowStageRole",
    "activity_engine_entries",
    "engine_catalog",
    "find_engine_catalog_entry",
    "find_engine_catalog_entry_by_app_id",
    "find_engine_catalog_entry_by_source_id",
    "get_engine_catalog_entry",
    "known_engine_ids",
    "supervised_engine_entries",
    "workflow_stage_engine_entries",
]
