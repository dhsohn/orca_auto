from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStoreCorruptError, active_slot_count
from orca_auto.core.config.files import (
    YAML_CONFIG_LOAD_EXCEPTIONS,
    engine_config_mapping,
    load_yaml_mapping,
    mapping_section,
    scheduler_admission_root,
    workflow_root_from_mapping,
)

from . import _common as _runtime_common

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


def submission_admission_limit_from_config(
    config_path: str | Path,
    *,
    positive_int_fn: Callable[[Any], int | None] = _runtime_common.positive_int,
) -> int | None:
    try:
        _, raw = load_yaml_mapping(config_path)
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

    scheduler_limit = positive_int_fn(effective_scheduler.get("max_active_simulations"))
    if scheduler_limit is not None:
        return scheduler_limit
    return None


def _submission_admission_root_from_config(
    config_path: str | Path,
    *,
    engine: str | None = None,
) -> Path | None:
    try:
        path, raw = load_yaml_mapping(config_path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None

    if engine in {"xtb", "crest"}:
        workflow_root = workflow_root_from_mapping(raw)
        if not workflow_root:
            return None
        return scheduler_admission_root(
            path,
            mapping_section(raw, "scheduler"),
            default_when_missing=True,
        )

    if engine:
        raw = engine_config_mapping(raw, engine, inherit_keys=("scheduler", "workflow"))
    if engine == "orca":
        scheduler = mapping_section(raw, "scheduler")
        return scheduler_admission_root(
            path,
            scheduler,
            default_when_missing=bool(scheduler),
        )
    return scheduler_admission_root(
        path,
        mapping_section(raw, "scheduler"),
        default_when_missing=bool(mapping_section(raw, "scheduler")),
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
        _, raw = load_yaml_mapping(config_path)
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
    for engine in (None, "xtb", "crest", "orca"):
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
    normalize_text_fn: Callable[[Any], str] = _runtime_common.normalize_text,
) -> bool:
    has_capacity_fn = submission_admission_has_capacity_fn or submission_admission_has_capacity
    for config_path in config_paths:
        config_text = normalize_text_fn(config_path)
        if not config_text:
            continue
        has_capacity = has_capacity_fn(config_text)
        if has_capacity is not None:
            return has_capacity
    return True
