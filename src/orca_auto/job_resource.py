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
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStoreCorruptError, read_slots
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.utils import process_lock
from orca_auto.flow.engine_runtime import engine_runtime_paths

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobProcessIdentity:
    """Boot-scoped identity for one queue job's engine process group."""

    queue_id: str
    pid: int
    pgid: int
    start_ticks: int
    boot_id: str


def live_job_processes(
    shared_config: str | None,
    *,
    engine_runtime_paths_fn: Callable[..., dict[str, Any]] = engine_runtime_paths,
    read_slots_fn: Callable[[Any], list[Any]] = read_slots,
    is_alive: Callable[[int], bool] = process_lock.is_process_alive,
    start_ticks: Callable[[int], int | None] = process_lock.process_start_ticks,
    boot_id: Callable[[], str | None] = process_lock.current_boot_id,
) -> list[JobProcessIdentity]:
    """Return unambiguous, validated-live engine identities.

    A passive metric is omitted unless its recorded and observed boot IDs match,
    the process leader is live, and its start ticks still match. Duplicate queue
    IDs or process groups are ambiguous and therefore omitted rather than being
    resolved by iteration order.
    """

    config_text = (shared_config or "").strip()
    if not config_text:
        return []
    try:
        # All engines share one admission root derived from ``runs_root``; the
        # ``engine`` arg only selects which config block supplies it, so "orca"
        # resolves the same root every engine's slots live in.
        runtime_paths = engine_runtime_paths_fn(config_text, engine="orca")
    except YAML_CONFIG_LOAD_EXCEPTIONS as exc:
        LOGGER.debug("job_processes_runtime_paths_failed: error=%s", exc)
        return []
    admission_root = runtime_paths.get("admission_root")
    if not isinstance(admission_root, Path):
        return []
    try:
        slots = read_slots_fn(admission_root)
    except (AdmissionStoreCorruptError, OSError) as exc:
        LOGGER.debug("job_processes_read_slots_failed: root=%s error=%s", admission_root, exc)
        return []

    observed_boot = boot_id()
    if not isinstance(observed_boot, str) or not observed_boot.strip():
        return []
    observed_boot = observed_boot.strip()

    candidates: list[JobProcessIdentity] = []
    for slot in slots:
        queue_id = str(getattr(slot, "queue_id", "") or "").strip()
        pid = getattr(slot, "engine_pid", None)
        pgid = getattr(slot, "engine_pgid", None)
        expected_ticks = getattr(slot, "engine_process_start_ticks", None)
        expected_boot = getattr(slot, "engine_process_boot_id", None)
        if (
            not queue_id
            or type(pid) is not int
            or pid <= 0
            or type(pgid) is not int
            or pgid <= 0
            or pgid != pid
            or type(expected_ticks) is not int
            or expected_ticks <= 0
            or not isinstance(expected_boot, str)
            or not expected_boot.strip()
            or expected_boot.strip() != observed_boot
        ):
            continue
        if not is_alive(pid) or start_ticks(pid) != expected_ticks:
            continue
        candidates.append(
            JobProcessIdentity(
                queue_id=queue_id,
                pid=pid,
                pgid=pgid,
                start_ticks=expected_ticks,
                boot_id=observed_boot,
            )
        )

    queue_counts = Counter(identity.queue_id for identity in candidates)
    pgid_counts = Counter(identity.pgid for identity in candidates)
    return [
        identity
        for identity in candidates
        if queue_counts[identity.queue_id] == 1 and pgid_counts[identity.pgid] == 1
    ]


__all__ = ["JobProcessIdentity", "live_job_processes"]
