"""Removing a visible generation that never acquired a durable queue owner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.queue.engine.input_snapshot import (
    cleanup_unowned_direct_generation_directory,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
    discard_snapshot_intent_if_generations_absent,
)
from orca_auto.core.queue.generation import is_visible_generation_name


def cleanup_unowned_orca_execution_snapshot(job_dir: str | Path, snapshot: Any) -> None:
    """Remove a visible generation that never acquired a durable queue owner."""

    if not isinstance(snapshot, Mapping):
        return
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    job_dir_identity = snapshot.get("job_dir_identity")
    if not isinstance(job_dir_identity, Mapping):
        raise ValueError("Refusing to clean an ORCA snapshot without job identity")
    job_dir_stat = resolved_job_dir.stat()
    if (int(job_dir_stat.st_dev), int(job_dir_stat.st_ino)) != (
        int(job_dir_identity.get("device", -1)),
        int(job_dir_identity.get("inode", -1)),
    ):
        raise ValueError("Refusing to clean an ORCA snapshot from a changed job directory")
    expected_job_identity = (int(job_dir_stat.st_dev), int(job_dir_stat.st_ino))
    namespace = str(snapshot.get("generation_name") or "").strip()
    raw_execution_dir = Path(str(snapshot.get("execution_dir") or "")).expanduser()
    execution_dir = resolved_job_dir / namespace
    if not is_visible_generation_name(namespace) or raw_execution_dir != execution_dir:
        raise ValueError("Refusing to clean a mismatched ORCA execution generation")
    if (
        execution_dir.is_symlink()
        or raw_execution_dir.is_symlink()
        or execution_dir.parent != resolved_job_dir
    ):
        raise ValueError("Refusing to clean an unconfined ORCA execution snapshot")
    raw_generation_identity = snapshot.get("execution_dir_identity")
    if not isinstance(raw_generation_identity, Mapping):
        raise ValueError("Refusing to clean an ORCA snapshot without generation identity")
    expected_generation_identity = (
        int(raw_generation_identity.get("device", -1)),
        int(raw_generation_identity.get("inode", -1)),
    )
    intent_token = str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    intent_root = str(snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or "").strip()
    if not intent_token or not intent_root:
        raise ValueError("Refusing to clean an ORCA snapshot without owner identity")
    cleanup_succeeded = False
    try:
        cleanup_unowned_direct_generation_directory(
            resolved_job_dir,
            namespace=namespace,
            label="ORCA execution snapshot",
            expected_job_identity=expected_job_identity,
            expected_generation_identity=expected_generation_identity,
            expected_owner_token=intent_token,
        )
        cleanup_succeeded = True
    finally:
        if cleanup_succeeded and intent_token and intent_root:
            discard_snapshot_intent_if_generations_absent(intent_root, intent_token)
