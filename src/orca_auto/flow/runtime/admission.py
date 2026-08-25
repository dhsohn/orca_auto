from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStoreCorruptError, active_slot_count
from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    engine_config_mapping,
    load_shared_config_mapping,
    mapping_section,
    scheduler_admission_root,
    usable_runs_root_from_mapping,
)
from orca_auto.core.engine_catalog import (
    engine_catalog,
    find_engine_catalog_entry,
)
from orca_auto.core.utils.coercion import normalize_text, positive_int

LOGGER = logging.getLogger(__name__)
_SUBMISSION_ADMISSION_CONFIG_KEYS = frozenset({"admission_root", "max_active_simulations"})


def _scheduler_section_has_admission_controls(scheduler: dict[str, Any]) -> bool:
    return any(key in scheduler for key in _SUBMISSION_ADMISSION_CONFIG_KEYS)


def _submission_admission_configured(raw: dict[str, Any]) -> bool:
    top_level_scheduler = mapping_section(raw, "scheduler")
    if _scheduler_section_has_admission_controls(top_level_scheduler):
        return True
    orca_raw = engine_config_mapping(raw, "orca", inherit_keys=("scheduler", "workflow"))
    return _scheduler_section_has_admission_controls(mapping_section(orca_raw, "scheduler"))


def submission_admission_limit_from_config(config_path: str | Path) -> int | None:
    try:
        _, raw = load_shared_config_mapping(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        LOGGER.debug(
            "submission_admission_limit_config_load_failed: config_path=%s error=%s",
            config_path,
            exc,
        )
        return None

    top_level_scheduler = mapping_section(raw, "scheduler")
    orca_raw = engine_config_mapping(raw, "orca", inherit_keys=("scheduler", "workflow"))
    effective_scheduler = mapping_section(orca_raw, "scheduler") or top_level_scheduler

    scheduler_limit = positive_int(effective_scheduler.get("max_active_simulations"))
    if scheduler_limit is not None:
        return scheduler_limit
    return None


def _submission_admission_root_from_config(
    config_path: str | Path,
    *,
    engine: str | None = None,
) -> Path | None:
    try:
        _, raw = load_shared_config_mapping(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None

    # The shared runs root anchors the default admission directory
    # (<runs_root>/.admission) for every engine. An invalid root is ignored
    # rather than resolved against the caller cwd, so the capacity gate never
    # counts slots in a store the workers themselves would reject.
    runs_root = usable_runs_root_from_mapping(raw)

    catalog_entry = find_engine_catalog_entry(engine)
    if catalog_entry is not None and catalog_entry.workflow_stage_role == "workflow-stage":
        if not runs_root:
            return None
        return scheduler_admission_root(
            mapping_section(raw, "scheduler"),
            default_runs_root=runs_root,
        )

    if engine:
        raw = engine_config_mapping(raw, engine, inherit_keys=("scheduler", "workflow"))
    return scheduler_admission_root(
        mapping_section(raw, "scheduler"),
        default_runs_root=runs_root,
    )


def submission_admission_has_capacity(
    config_path: str | Path,
    *,
    submission_admission_limit_from_config_fn: Callable[
        [str | Path], int | None
    ] = submission_admission_limit_from_config,
    active_slot_count_fn: Callable[[Path], int] = active_slot_count,
    engine_runtime_paths_fn: Callable[..., dict[str, Any]] | None = None,
) -> bool | None:
    try:
        _, raw = load_shared_config_mapping(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        LOGGER.debug(
            "submission_admission_capacity_config_load_failed: config_path=%s error=%s",
            config_path,
            exc,
        )
        return False

    admission_configured = _submission_admission_configured(raw)
    limit = submission_admission_limit_from_config_fn(config_path)
    if limit is None:
        return False if admission_configured else None
    admission_root: Path | None = None
    managed_entries = tuple(entry for entry in engine_catalog() if entry.managed_admission)
    workflow_stage_ids = {
        entry.engine_id
        for entry in managed_entries
        if entry.workflow_stage_role == "workflow-stage"
    }
    admission_engine_ids = (
        *(
            entry.engine_id
            for entry in managed_entries
            if entry.workflow_stage_role == "workflow-stage"
        ),
        *(
            entry.engine_id
            for entry in managed_entries
            if entry.engine_id not in workflow_stage_ids
        ),
    )
    for engine in (None, *admission_engine_ids):
        try:
            if engine_runtime_paths_fn is None:
                candidate = _submission_admission_root_from_config(config_path, engine=engine)
            else:
                runtime_paths = engine_runtime_paths_fn(str(config_path), engine=engine)
                candidate = runtime_paths.get("admission_root")
        except (OSError, ValueError) as exc:
            LOGGER.debug(
                "submission_admission_root_lookup_failed: config_path=%s engine=%s error=%s",
                config_path,
                engine,
                exc,
            )
            continue
        if isinstance(candidate, Path):
            admission_root = candidate
            break
    if not isinstance(admission_root, Path):
        return False if admission_configured else None
    try:
        return active_slot_count_fn(admission_root) < limit
    except (AdmissionStoreCorruptError, OSError) as exc:
        LOGGER.debug(
            "submission_admission_slot_count_failed: admission_root=%s error=%s",
            admission_root,
            exc,
        )
        return False


def workflow_submission_has_capacity(
    *config_paths: str | Path | None,
    submission_admission_has_capacity_fn: Callable[[str | Path], bool | None] | None = None,
) -> bool:
    has_capacity_fn = submission_admission_has_capacity_fn or submission_admission_has_capacity
    for config_path in config_paths:
        config_text = normalize_text(config_path)
        if not config_text:
            continue
        has_capacity = has_capacity_fn(config_text)
        if has_capacity is not None:
            return has_capacity
    return True
