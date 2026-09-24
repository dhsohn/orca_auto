"""Reserving one visible generation directory under a durable snapshot intent."""

from __future__ import annotations

from pathlib import Path

from orca_auto.core.queue.engine.input_snapshot import (
    cleanup_unowned_direct_generation_directory,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    bind_snapshot_intent_generation_identities,
    create_snapshot_intent,
    discard_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
)
from orca_auto.core.queue.generation import (
    is_visible_generation_name,
    new_visible_generation_name,
)
from orca_auto.core.utils.persistence import fsync_directory


def _execution_directory(job_dir: Path, generation_name: str) -> Path:
    if not is_visible_generation_name(generation_name):
        raise ValueError("ORCA generation name is invalid")
    execution_dir = job_dir / generation_name
    if execution_dir.is_symlink():
        raise ValueError(f"ORCA generation must not be a symlink: {execution_dir}")
    execution_dir.mkdir(mode=0o700, exist_ok=False)
    fsync_directory(job_dir)
    return execution_dir.resolve()


def _reserve_execution_generation(
    job_dir: Path,
    *,
    queue_root: Path,
    intent_token: str,
    target_generation_name: str | None = None,
) -> tuple[str, Path, tuple[int, int]]:
    if target_generation_name is not None and not is_visible_generation_name(
        target_generation_name
    ):
        raise ValueError("ORCA execution snapshot target generation name is invalid")
    job_status = job_dir.stat()
    job_identity = (int(job_status.st_dev), int(job_status.st_ino))
    for _attempt in range(1 if target_generation_name is not None else 32):
        generation_name = target_generation_name or new_visible_generation_name()
        execution_dir = job_dir / generation_name
        try:
            create_snapshot_intent(
                queue_root,
                token=intent_token,
                kind="orca_visible_generation",
                generation_paths=[execution_dir],
            )
        except FileExistsError:
            if execution_dir.exists() or execution_dir.is_symlink():
                continue
            raise
        try:
            reserved = _execution_directory(job_dir, generation_name)
        except FileExistsError:
            discard_snapshot_intent(queue_root, intent_token)
            continue
        except BaseException:
            discard_snapshot_intent(queue_root, intent_token)
            raise
        details = reserved.stat()
        generation_identity = (int(details.st_dev), int(details.st_ino))
        try:
            bind_snapshot_intent_generation_identities(queue_root, intent_token)
        except BaseException:
            try:
                cleanup_unowned_direct_generation_directory(
                    job_dir,
                    namespace=generation_name,
                    label="ORCA execution snapshot",
                    expected_job_identity=job_identity,
                    expected_generation_identity=generation_identity,
                    expected_owner_token=intent_token,
                )
            finally:
                discard_snapshot_intent_if_generations_absent(queue_root, intent_token)
            raise
        return generation_name, reserved, generation_identity
    if target_generation_name is not None:
        raise FileExistsError(
            "ORCA crash recovery target generation already exists: "
            f"{job_dir / target_generation_name}; inspect and remove it before "
            "resubmitting the job"
        )
    raise FileExistsError("Could not reserve a unique visible ORCA generation directory")
