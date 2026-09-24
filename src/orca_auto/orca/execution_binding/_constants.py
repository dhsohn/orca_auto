"""Snapshot schema version and byte budget shared by every binding stage."""

from __future__ import annotations

from orca_auto.core.queue.engine.input_snapshot import MAX_INPUT_SNAPSHOT_BYTES

ORCA_EXECUTION_SNAPSHOT_VERSION = 3


MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES = 4 * MAX_INPUT_SNAPSHOT_BYTES
