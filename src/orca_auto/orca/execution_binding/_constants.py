"""Snapshot schema version, byte budget and names shared by every binding stage."""

from __future__ import annotations

from pathlib import Path

from orca_auto.core.confined_io import MAX_INPUT_SNAPSHOT_BYTES

ORCA_EXECUTION_SNAPSHOT_VERSION = 3


MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES = 4 * MAX_INPUT_SNAPSHOT_BYTES


def resume_checkpoint_input_path(selected_inp: Path) -> Path:
    """``<stem>.resume.inp``, the input the removed attempt-level resume wrote.

    Generations from before the removal may hold it and its outputs, so a
    dependency may not take these names and crash recovery still seeds from
    its checkpoint.
    """
    return selected_inp.with_name(f"{selected_inp.stem}.resume.inp")
