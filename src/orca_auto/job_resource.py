"""Resolve live per-job engine process groups from admission slots.

Read-only helper for the ``queue list --watch`` per-job CPU/RAM view. The queue
worker already records each running job's engine PID/PGID — with a boot-id and
process-start-tick fence — in the durable admission-slot store, and ORCA, the
internal xTB/CREST queue, and standalone xTB-MD all register it. So the CLI can
attribute resource usage per job without any worker or durable-state change.

Everything fails closed: a missing config, an unreadable/corrupt slot store, or an
engine identity that no longer validates (wrong boot, dead PID, or a reused PID
whose start ticks do not match) simply drops that job, so a reused PID/PGID is
never mis-attributed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStoreCorruptError, read_slots
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.utils import process_lock
from orca_auto.flow.engine_runtime import engine_runtime_paths

LOGGER = logging.getLogger(__name__)


def _engine_process_alive(
    slot: Any,
    *,
    is_alive: Callable[[int], bool],
    start_ticks: Callable[[int], int | None],
    boot_id: Callable[[], str | None],
) -> bool:
    pid = getattr(slot, "engine_pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    expected_boot = getattr(slot, "engine_process_boot_id", None)
    if isinstance(expected_boot, str) and expected_boot.strip():
        observed_boot = boot_id()
        if (
            isinstance(observed_boot, str)
            and observed_boot.strip()
            and observed_boot.strip() != expected_boot.strip()
        ):
            return False  # recorded on a previous boot -> not our live process
    if not is_alive(pid):
        return False
    expected_ticks = getattr(slot, "engine_process_start_ticks", None)
    if isinstance(expected_ticks, int) and expected_ticks > 0:
        observed_ticks = start_ticks(pid)
        if observed_ticks is None or observed_ticks != expected_ticks:
            return False  # PID reused by a different process
    return True


def live_job_pgids(
    shared_config: str | None,
    *,
    engine_runtime_paths_fn: Callable[..., dict[str, Any]] = engine_runtime_paths,
    read_slots_fn: Callable[[Any], list[Any]] = read_slots,
    is_alive: Callable[[int], bool] = process_lock.is_process_alive,
    start_ticks: Callable[[int], int | None] = process_lock.process_start_ticks,
    boot_id: Callable[[], str | None] = process_lock.current_boot_id,
) -> dict[str, int]:
    """Return ``{queue_id: engine_pgid}`` for slots with a validated-live engine.

    Only process groups whose recorded engine identity still validates are
    returned, so aggregating ``/proc`` by these PGIDs cannot pick up an unrelated
    process that reused the id.
    """

    config_text = (shared_config or "").strip()
    if not config_text:
        return {}
    try:
        # All engines share one admission root derived from ``runs_root``; the
        # ``engine`` arg only selects which config block supplies it, so "orca"
        # resolves the same root every engine's slots live in.
        runtime_paths = engine_runtime_paths_fn(config_text, engine="orca")
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        LOGGER.debug("job_pgids_runtime_paths_failed: error=%s", exc)
        return {}
    admission_root = runtime_paths.get("admission_root")
    if not isinstance(admission_root, Path):
        return {}
    try:
        slots = read_slots_fn(admission_root)
    except (AdmissionStoreCorruptError, OSError) as exc:
        LOGGER.debug("job_pgids_read_slots_failed: root=%s error=%s", admission_root, exc)
        return {}

    result: dict[str, int] = {}
    for slot in slots:
        queue_id = str(getattr(slot, "queue_id", "") or "").strip()
        pgid = getattr(slot, "engine_pgid", None)
        if not queue_id or not isinstance(pgid, int) or pgid <= 0:
            continue
        if not _engine_process_alive(
            slot, is_alive=is_alive, start_ticks=start_ticks, boot_id=boot_id
        ):
            continue
        result[queue_id] = pgid
    return result


__all__ = ["live_job_pgids"]
