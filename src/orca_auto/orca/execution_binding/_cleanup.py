"""Removing a visible generation that never acquired a durable queue owner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.generation_owner import (
    cleanup_unowned_direct_generation_directory,
)
from orca_auto.core.queue.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
    discard_snapshot_intent_if_generations_absent,
)

from ._snapshot_identity import same_directory_identity


def discard_unowned_generation(
    job_dir: Path,
    *,
    generation_name: str,
    job_identity: tuple[int, int],
    generation_identity: tuple[int, int],
    owner_token: str,
    queue_root: str | Path,
    discard_on_failure: bool,
) -> None:
    """Remove one generation its intent still owns, then discard the intent.

    The intent is discarded only once no declared generation remains. After a
    failed removal it is discarded as well when ``discard_on_failure`` is set:
    a build or reservation that is unwinding tries both, while a cleanup of a
    built snapshot leaves the intent for the worker's orphan pass.
    """
    try:
        cleanup_unowned_direct_generation_directory(
            job_dir,
            namespace=generation_name,
            label="ORCA execution snapshot",
            expected_job_identity=job_identity,
            expected_generation_identity=generation_identity,
            expected_owner_token=owner_token,
        )
    except BaseException:
        if discard_on_failure:
            discard_snapshot_intent_if_generations_absent(queue_root, owner_token)
        raise
    discard_snapshot_intent_if_generations_absent(queue_root, owner_token)


def cleanup_unowned_orca_execution_snapshot(job_dir: str | Path, snapshot: Any) -> None:
    """Remove a visible generation that never acquired a durable queue owner."""

    if not isinstance(snapshot, Mapping):
        return
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    job_dir_identity = snapshot.get("job_dir_identity")
    if not isinstance(job_dir_identity, Mapping):
        raise ValueError("Refusing to clean an ORCA snapshot without job identity")
    job_dir_stat = resolved_job_dir.stat()
    if not same_directory_identity(job_dir_stat, job_dir_identity):
        raise ValueError("Refusing to clean an ORCA snapshot from a changed job directory")
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
    generation_identity = (
        int(raw_generation_identity.get("device", -1)),
        int(raw_generation_identity.get("inode", -1)),
    )
    intent_token = str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    intent_root = str(snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or "").strip()
    if not intent_token or not intent_root:
        raise ValueError("Refusing to clean an ORCA snapshot without owner identity")
    discard_unowned_generation(
        resolved_job_dir,
        generation_name=namespace,
        job_identity=(int(job_dir_stat.st_dev), int(job_dir_stat.st_ino)),
        generation_identity=generation_identity,
        owner_token=intent_token,
        queue_root=intent_root,
        discard_on_failure=False,
    )
