from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from orca_auto.core.queue.engine.input_snapshot import (
    SNAPSHOT_DIR_NAME,
    cleanup_unowned_input_snapshot_namespace,
    reserve_input_snapshot_namespace,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    INPUT_SNAPSHOT_NAMESPACE_INTENT_KIND,
    create_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
)

_SubmissionT = TypeVar("_SubmissionT")


def build_reserved_input_snapshot_submission(
    cfg: Any,
    job_dir: Any,
    manifest: dict[str, Any],
    args: Any,
    *,
    new_job_id_fn: Callable[[], str],
    queue_root_for_path_fn: Callable[[Any, Path], Path],
    build_submission_fn: Callable[..., _SubmissionT],
) -> _SubmissionT:
    """Build after reserving one intent-tracked input-snapshot namespace."""

    job_id = new_job_id_fn()
    snapshot_namespace = f"snapshot-{secrets.token_hex(16)}"
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    queue_root = (
        queue_root_for_path_fn(cfg, resolved_job_dir)
        if getattr(cfg, "runtime", None) is not None
        else resolved_job_dir
    )
    generation_path = resolved_job_dir / SNAPSHOT_DIR_NAME / snapshot_namespace
    reserved = False
    intent_created = False
    try:
        create_snapshot_intent(
            queue_root,
            token=snapshot_namespace,
            kind=INPUT_SNAPSHOT_NAMESPACE_INTENT_KIND,
            generation_paths=[generation_path],
        )
        intent_created = True
        reserve_input_snapshot_namespace(job_dir, snapshot_namespace)
        reserved = True
        return build_submission_fn(
            cfg,
            job_dir,
            manifest,
            args,
            job_id=job_id,
            snapshot_namespace=snapshot_namespace,
        )
    except BaseException:
        if reserved:
            cleanup_unowned_input_snapshot_namespace(job_dir, snapshot_namespace)
        if intent_created:
            discard_snapshot_intent_if_generations_absent(queue_root, snapshot_namespace)
        raise


__all__ = ["build_reserved_input_snapshot_submission"]
