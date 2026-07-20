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

from .path_identity import validate_execution_snapshot_job_dir

XTB_MD_VISIBLE_GENERATION_KIND = "xtb_md_visible_generation"


def reserve_execution_generation(
    job_dir: Path,
    *,
    queue_root: Path,
    intent_token: str,
) -> tuple[str, Path, tuple[int, int]]:
    resolved_job_dir = job_dir.expanduser().resolve()
    job_status = resolved_job_dir.stat()
    job_identity = (int(job_status.st_dev), int(job_status.st_ino))
    for _attempt in range(32):
        generation_name = new_visible_generation_name()
        execution_dir = resolved_job_dir / generation_name
        try:
            create_snapshot_intent(
                queue_root,
                token=intent_token,
                kind=XTB_MD_VISIBLE_GENERATION_KIND,
                generation_paths=[execution_dir],
            )
        except FileExistsError:
            if execution_dir.exists() or execution_dir.is_symlink():
                continue
            raise
        try:
            execution_dir.mkdir(mode=0o700, exist_ok=False)
            fsync_directory(resolved_job_dir)
        except FileExistsError:
            discard_snapshot_intent(queue_root, intent_token)
            continue
        except BaseException:
            discard_snapshot_intent(queue_root, intent_token)
            raise

        resolved_execution_dir = execution_dir.resolve()
        details = resolved_execution_dir.stat()
        generation_identity = (int(details.st_dev), int(details.st_ino))
        try:
            bind_snapshot_intent_generation_identities(queue_root, intent_token)
        except BaseException:
            try:
                cleanup_unowned_direct_generation_directory(
                    resolved_job_dir,
                    namespace=generation_name,
                    label="xTB-MD execution snapshot",
                    expected_job_identity=job_identity,
                    expected_generation_identity=generation_identity,
                    expected_owner_token=intent_token,
                )
            finally:
                discard_snapshot_intent_if_generations_absent(queue_root, intent_token)
            raise
        return generation_name, resolved_execution_dir, generation_identity
    raise FileExistsError("Could not reserve a unique visible xTB-MD generation directory")


def _execution_snapshot_generation_dir(
    job_dir: str | Path,
    snapshot: Any,
) -> Path:
    if not isinstance(snapshot, Mapping) or snapshot.get("version") != 2:
        raise ValueError("xTB-MD execution snapshot has an unsupported version")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    execution_text = str(snapshot.get("execution_dir") or "").strip()
    raw_execution_dir = Path(execution_text).expanduser()
    if not execution_text or not raw_execution_dir.is_absolute():
        raise ValueError("Queued xTB-MD execution directory is invalid")
    execution_dir = raw_execution_dir.resolve()
    if (
        raw_execution_dir.is_symlink()
        or raw_execution_dir != execution_dir
        or not execution_dir.is_dir()
        or execution_dir.parent != resolved_job_dir
        or not is_visible_generation_name(execution_dir.name)
        or str(snapshot.get("generation_name") or "") != execution_dir.name
    ):
        raise ValueError("Queued xTB-MD execution directory escapes its job directory")

    raw_identity = snapshot.get("execution_dir_identity")
    if not isinstance(raw_identity, Mapping):
        raise ValueError("Queued xTB-MD generation has no directory identity")
    generation_identity = (
        int(raw_identity.get("device", -1)),
        int(raw_identity.get("inode", -1)),
    )
    details = execution_dir.stat()
    if (int(details.st_dev), int(details.st_ino)) != generation_identity:
        raise ValueError("Queued xTB-MD generation directory identity changed")

    return execution_dir


def validate_xtb_md_generation(
    configured_runs_root: str | Path,
    snapshot: Any,
) -> Path:
    if not isinstance(snapshot, Mapping):
        raise ValueError("xTB-MD execution snapshot must be an object")
    job_dir = validate_execution_snapshot_job_dir(configured_runs_root, snapshot)
    return _execution_snapshot_generation_dir(job_dir, snapshot)


def cleanup_unowned_execution_generation(
    job_dir: str | Path,
    snapshot: Any,
) -> None:
    if not isinstance(snapshot, Mapping) or snapshot.get("version") != 2:
        raise ValueError("Refusing to clean an unbound xTB-MD execution snapshot")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    generation_name = str(snapshot.get("generation_name") or "").strip()
    raw_identity = snapshot.get("execution_dir_identity")
    raw_job_identity = snapshot.get("job_path_identity")
    job_descriptor = (
        raw_job_identity.get("job_dir") if isinstance(raw_job_identity, Mapping) else None
    )
    owner_token = str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    if (
        not is_visible_generation_name(generation_name)
        or not isinstance(raw_identity, Mapping)
        or not isinstance(job_descriptor, Mapping)
        or not owner_token
    ):
        raise ValueError("Refusing to clean an unbound xTB-MD execution snapshot")
    job_identity = (
        int(job_descriptor.get("device", -1)),
        int(job_descriptor.get("inode", -1)),
    )
    job_details = resolved_job_dir.stat()
    if (int(job_details.st_dev), int(job_details.st_ino)) != job_identity:
        raise ValueError("xTB-MD cleanup job directory identity changed")
    generation_identity = (
        int(raw_identity.get("device", -1)),
        int(raw_identity.get("inode", -1)),
    )
    cleanup_unowned_direct_generation_directory(
        resolved_job_dir,
        namespace=generation_name,
        label="xTB-MD execution snapshot",
        expected_job_identity=job_identity,
        expected_generation_identity=generation_identity,
        expected_owner_token=owner_token,
    )
    intent_root = Path(str(snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or "")).expanduser()
    if not intent_root.is_absolute():
        raise ValueError("xTB-MD snapshot intent root is invalid")
    discard_snapshot_intent_if_generations_absent(intent_root.resolve(), owner_token)


__all__ = [
    "XTB_MD_VISIBLE_GENERATION_KIND",
    "cleanup_unowned_execution_generation",
    "reserve_execution_generation",
    "validate_xtb_md_generation",
]
