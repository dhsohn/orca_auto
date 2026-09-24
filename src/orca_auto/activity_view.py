from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStoreCorruptError, read_active_slot_count
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.utils import normalize_text
from orca_auto.orca.engine_runtime import engine_runtime_paths

LOGGER = logging.getLogger(__name__)


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


def global_active_simulations(
    *, config_path: str | None, fallback: int
) -> tuple[int, dict[str, Any] | None]:
    """The admission store's live slot count, else the catalog's own count.

    The slot count is the global truth across every consumer of the runtime;
    ``fallback`` is the listing's count of active job rows, used only when no
    admission root is configured or its store cannot be read. A corrupt store
    is returned as an ``admission_blockers`` payload next to the fallback
    count: the worker admits nothing while that file does not load.
    """
    config_text = normalize_text(config_path)
    if config_text:
        try:
            runtime_paths = engine_runtime_paths(config_text)
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
                return max(0, int(read_active_slot_count(admission_root))), None
            except AdmissionStoreCorruptError as exc:
                return max(0, int(fallback)), {
                    "queue_id": "*",
                    "allowed_root": str(runtime_paths.get("allowed_root", "")),
                    "scope": "admission_store",
                    "reason": str(exc),
                    "next_action": (
                        "Repair or remove the admission slot file; "
                        "the worker admits no job until it loads."
                    ),
                }
            except OSError as exc:
                LOGGER.debug(
                    "active_simulation_slot_count_failed: admission_root=%s error=%s",
                    admission_root,
                    exc,
                )
    return max(0, int(fallback)), None
