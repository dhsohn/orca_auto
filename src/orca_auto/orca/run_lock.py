from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.process_tracking import (
    RUN_LOCK_FILE_NAME,
    current_process_lock_payload,
    run_lock_status,
)

logger = logging.getLogger(__name__)

# ``run_lock_status`` probes briefly flock a free run.lock; the wait covers
# that window, while a real holder keeps the lock far longer.
_RUN_LOCK_ACQUIRE_TIMEOUT_SECONDS = 1.0


@contextmanager
def acquire_run_lock(reaction_dir: Path) -> Iterator[None]:
    lock_path = reaction_dir / RUN_LOCK_FILE_NAME
    payload = json.dumps(current_process_lock_payload(), ensure_ascii=True)
    try:
        with file_lock(
            lock_path, timeout_seconds=_RUN_LOCK_ACQUIRE_TIMEOUT_SECONDS, payload=payload
        ):
            logger.debug("Lock acquired: %s", lock_path)
            try:
                yield
            finally:
                logger.debug("Lock released: %s", lock_path)
    except TimeoutError:
        status = run_lock_status(reaction_dir, logger=logger)
        owner = f"pid={status.pid}" if status.pid is not None else "pid=unknown"
        started = status.started_at or "unknown"
        raise RuntimeError(
            "Another orca_auto instance is already running in this directory "
            f"({owner}, started_at={started}). Lock file: {lock_path}"
        ) from None
