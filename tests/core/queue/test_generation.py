from __future__ import annotations

from dataclasses import replace

from orca_auto.core.queue import store
from orca_auto.core.queue.generation import (
    queue_entries_same_generation,
    queue_entry_generation_token,
)
from orca_auto.core.queue.publication import QUEUE_RECORD_SYNC_UPDATED_AT_KEY
from orca_auto.core.queue.types import QueueStatus


def test_queue_generation_token_tracks_only_immutable_identity() -> None:
    entry = store.QueueEntry(
        queue_id="q-generation",
        app_name="orca_auto_orca",
        task_id="orca-task",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.PENDING,
        enqueued_at="2026-04-19T00:00:00+00:00",
        metadata={
            "job_dir": "/runs/water-md",
            "resource_request": {"max_cores": 2, "max_memory_gb": 4},
            "resource_actual": {"max_cores": 2, "max_memory_gb": 4},
            "execution_snapshot": {
                "version": 2,
                "generation_name": "20260419-000000-a1b2c3d4",
                "execution_dir": "/runs/water-md/20260419-000000-a1b2c3d4",
                "execution_dir_identity": {"device": 11, "inode": 22},
                "selected_input_xyz": "/runs/water-md/20260419-000000-a1b2c3d4/input.xyz",
            },
            "retry_supported": False,
            "resume_supported": False,
        },
    )
    token = queue_entry_generation_token(entry)

    running = replace(
        entry,
        status=QueueStatus.RUNNING,
        started_at="2026-04-19T00:01:00+00:00",
        metadata={
            **entry.metadata,
            "execution_dir": "/runs/water-md/20260419-000000-a1b2c3d4",
            "attempt": 1,
            "run_id": "run_20260419_runtime",
            "orca_terminal_replay": None,
            "orca_terminal_replay_fence_only": True,
        },
    )
    terminal = replace(
        running,
        status=QueueStatus.CANCELLED,
        finished_at="2026-04-19T00:02:00+00:00",
        cancel_requested=True,
        error="cancel_requested",
        metadata={
            **running.metadata,
            "terminal_artifacts": {
                "trajectory": {"path": "xtb.trj", "sha256": "a" * 64},
            },
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "later",
        },
    )

    assert queue_entry_generation_token(running) == token
    assert queue_entry_generation_token(terminal) == token
    assert queue_entries_same_generation(running, entry)
    assert queue_entries_same_generation(terminal, entry)


def test_queue_generation_rejects_immutable_metadata_changes() -> None:
    entry = store.QueueEntry(
        queue_id="q-generation",
        app_name="orca_auto_orca",
        task_id="orca-task",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.PENDING,
        enqueued_at="2026-04-19T00:00:00+00:00",
        metadata={
            "job_dir": "/runs/water-md",
            "resource_request": {"max_cores": 2, "max_memory_gb": 4},
            "resource_actual": {"max_cores": 2, "max_memory_gb": 4},
            "execution_snapshot": {
                "version": 2,
                "generation_name": "20260419-000000-a1b2c3d4",
                "execution_dir": "/runs/water-md/20260419-000000-a1b2c3d4",
                "execution_dir_identity": {"device": 11, "inode": 22},
                "selected_input_xyz": "/runs/water-md/20260419-000000-a1b2c3d4/input.xyz",
            },
            "retry_supported": False,
            "resume_supported": False,
        },
    )
    token = queue_entry_generation_token(entry)
    replacements = (
        replace(entry, metadata={**entry.metadata, "job_dir": "/runs/replacement"}),
        replace(
            entry,
            metadata={
                **entry.metadata,
                "resource_request": {"max_cores": 4, "max_memory_gb": 4},
            },
        ),
        replace(
            entry,
            metadata={
                **entry.metadata,
                "resource_actual": {"max_cores": 4, "max_memory_gb": 4},
            },
        ),
        replace(
            entry,
            metadata={
                **entry.metadata,
                "execution_snapshot": {
                    "version": 2,
                    "generation_name": "20260419-000001-deadbeef",
                    "execution_dir": "/runs/water-md/20260419-000001-deadbeef",
                    "execution_dir_identity": {"device": 11, "inode": 33},
                    "selected_input_xyz": "/runs/water-md/20260419-000001-deadbeef/input.xyz",
                },
            },
        ),
        replace(entry, metadata={**entry.metadata, "retry_supported": True}),
        replace(entry, metadata={**entry.metadata, "resume_supported": True}),
    )

    for replacement in replacements:
        assert queue_entry_generation_token(replacement) != token
        assert not queue_entries_same_generation(replacement, entry)
