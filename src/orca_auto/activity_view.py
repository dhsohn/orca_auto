from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStoreCorruptError, read_active_slot_count
from orca_auto.core.config.bounded_yaml import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.engine_runtime import engine_runtime_paths
from orca_auto.core.statuses import (
    STATUS_CANCEL_REQUESTED,
    STATUS_RETRYING,
    STATUS_RUNNING,
)
from orca_auto.core.utils import normalize_text

LOGGER = logging.getLogger(__name__)

ACTIVE_SIMULATION_STATUSES = frozenset({STATUS_RUNNING, STATUS_RETRYING, STATUS_CANCEL_REQUESTED})
ActivityItem = dict[str, Any]


def normalize_activity_filter_values(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(value).lower()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def filter_activity_items(
    items: Sequence[dict[str, Any]],
    *,
    engines: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    engine_filter = set(normalize_activity_filter_values(engines))
    status_filter = set(normalize_activity_filter_values(statuses))
    kind_filter = set(normalize_activity_filter_values(kinds))

    filtered: list[dict[str, Any]] = []
    for item in items:
        engine = normalize_text(item.get("engine")).lower()
        status = normalize_text(item.get("status")).lower()
        kind = normalize_text(item.get("kind")).lower()
        if engine_filter and engine not in engine_filter:
            continue
        if status_filter and status not in status_filter:
            continue
        if kind_filter and kind not in kind_filter:
            continue
        filtered.append(dict(item))
    return filtered


def count_active_simulations(items: Sequence[dict[str, Any]]) -> int:
    total = 0
    for item in items:
        if normalize_text(item.get("kind")).lower() != "job":
            continue
        status = normalize_text(item.get("status")).lower()
        if status in ACTIVE_SIMULATION_STATUSES:
            total += 1
    return total


def activity_counter_config_path(
    payload: dict[str, Any],
    *,
    config_hints: Sequence[str | None] = (),
    prefer_hints: bool = False,
) -> str | None:
    def first_source_config() -> str | None:
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            return None
        for key in ("orca_config",):
            source_text = normalize_text(sources.get(key))
            if source_text:
                return source_text
        return None

    def first_hint_config() -> str | None:
        for value in config_hints:
            text = normalize_text(value)
            if text:
                return text
        return None

    if prefer_hints:
        return first_hint_config() or first_source_config()
    return first_source_config() or first_hint_config()


def count_global_active_simulations(
    items: Sequence[dict[str, Any]],
    *,
    config_path: str | None = None,
) -> int:
    config_text = normalize_text(config_path)
    if config_text:
        try:
            runtime_paths = engine_runtime_paths(config_text, engine="orca")
        except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
            LOGGER.debug(
                "active_simulation_runtime_paths_failed: config_path=%s error=%s",
                config_text,
                exc,
            )
            runtime_paths = {}
        admission_root = runtime_paths.get("admission_root")
        if isinstance(admission_root, Path):
            try:
                return max(0, int(read_active_slot_count(admission_root)))
            except (AdmissionStoreCorruptError, OSError) as exc:
                LOGGER.debug(
                    "active_simulation_slot_count_failed: admission_root=%s error=%s",
                    admission_root,
                    exc,
                )
    return count_active_simulations(items)
