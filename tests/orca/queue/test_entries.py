"""``orca.queue.entries``: the ORCA row identity and the one generation identity."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from orca_auto.core.queue import store
from orca_auto.core.queue.deferral import ADMISSION_DEFERRAL_METADATA_KEY
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.queue.entries import (
    QUEUED_NOTIFICATION_PENDING_KEY,
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    is_orca_queue_entry,
    queue_entry_generation_token,
    same_generation,
)


def test_orca_identity_requires_every_label() -> None:
    own_entry = SimpleNamespace(
        queue_id="q-orca",
        app_name="orca_auto_orca",
        task_id="orca-1",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={"job_type": "opt"},
    )
    assert is_orca_queue_entry(own_entry)

    for overrides in (
        {"app_name": "orca_auto_other"},
        {"engine": "other"},
        {"queue_id": ""},
        {"task_id": ""},
        {"task_kind": ""},
        {"task_kind": "other_run"},
        {"app_name": "", "engine": ""},
    ):
        assert not is_orca_queue_entry(SimpleNamespace(**{**vars(own_entry), **overrides}))

    canonical_orca_mapping = {
        "queue_id": "q-orca",
        "app_name": "orca_auto_orca",
        "task_id": "orca-1",
        "task_kind": "orca_run_inp",
        "engine": "orca",
        "metadata": {},
    }
    assert is_orca_queue_entry(canonical_orca_mapping)
    for missing_field in ("app_name", "task_id", "task_kind", "engine"):
        partial = dict(canonical_orca_mapping)
        partial.pop(missing_field)
        assert not is_orca_queue_entry(partial)


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
            ADMISSION_DEFERRAL_METADATA_KEY: {"reason": "scratch full"},
            QUEUED_NOTIFICATION_PENDING_KEY: False,
            "run_id": "run_20260419_runtime",
            TERMINAL_REPLAY_METADATA_KEY: None,
            TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY: True,
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
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "later",
            QUEUE_RECORD_SYNC_BLOCKED_KEY: {"reason": "blocked"},
        },
    )

    assert queue_entry_generation_token(running) == token
    assert queue_entry_generation_token(terminal) == token
    assert same_generation(running, entry)
    assert same_generation(terminal, entry)


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
        # Keys no current writer sets are identity like any unknown key.
        replace(entry, metadata={**entry.metadata, "attempt": 1}),
        replace(entry, metadata={**entry.metadata, "execution_dir": "/runs/water-md/x"}),
        replace(entry, metadata={**entry.metadata, "candidate_count": 3}),
        replace(entry, enqueued_at="2026-04-19T00:00:01+00:00"),
    )

    for replacement in replacements:
        assert queue_entry_generation_token(replacement) != token
        assert not same_generation(replacement, entry)
