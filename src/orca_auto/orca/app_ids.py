"""Persisted ORCA identity labels.

Queue rows, admission slots, job locations and activity records store these
strings; changing one needs a migration of the records that carry it. The
module imports nothing, so CLI and activity code read it without the
execution stack.
"""

from __future__ import annotations

ORCA_ENGINE = "orca"
ORCA_TASK_KIND = "orca_run_inp"
ORCA_AUTO_ORCA_APP_NAME = "orca_auto_orca"
ORCA_AUTO_ORCA_SOURCE = "orca_auto_orca"
# The ``source`` label of every admission_slots.json reservation; not a module path.
ORCA_ADMISSION_SOURCE = "orca_auto.orca.queue_worker"
ORCA_ENGINE_LAUNCH_GATED = True

__all__ = [
    "ORCA_ADMISSION_SOURCE",
    "ORCA_AUTO_ORCA_APP_NAME",
    "ORCA_AUTO_ORCA_SOURCE",
    "ORCA_ENGINE",
    "ORCA_ENGINE_LAUNCH_GATED",
    "ORCA_TASK_KIND",
]
