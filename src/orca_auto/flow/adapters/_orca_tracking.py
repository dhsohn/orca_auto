from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.orca.job_locations import (
    load_job_artifact_context,
    load_job_runtime_context,
    load_orca_contract_payload,
)

LOGGER = logging.getLogger(__name__)


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def tracked_artifact_context_impl(
    *,
    index_root: Path | None,
    targets: tuple[str, ...],
) -> tuple[Path | None, JobLocationRecord | None, dict[str, Any], dict[str, Any]]:
    if index_root is None:
        return None, None, {}, {}

    for raw_target in targets:
        target = normalize_text(raw_target)
        if not target:
            continue
        try:
            context = load_job_artifact_context(index_root, target)
        except FileNotFoundError as exc:
            LOGGER.debug(
                "orca_artifact_context_load_failed: index_root=%s target=%s error=%s",
                index_root,
                target,
                exc,
            )
            continue
        if context.job_dir is None:
            continue
        return (
            context.job_dir,
            context.record,
            _dict_payload(context.state),
            _dict_payload(context.report),
        )
    return None, None, {}, {}


def tracked_runtime_context_impl(
    *,
    index_root: Path | None,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> (
    tuple[
        Path | None,
        JobLocationRecord | None,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any] | None,
    ]
    | None
):
    if index_root is None:
        return None

    try:
        context = load_job_runtime_context(
            index_root,
            target,
            queue_id=queue_id,
            run_id=run_id,
            reaction_dir=reaction_dir,
        )
    except FileNotFoundError:
        return None

    artifact = context.artifact
    queue_entry = context.queue_entry
    return (
        artifact.job_dir,
        artifact.record,
        _dict_payload(artifact.state),
        _dict_payload(artifact.report),
        dict(queue_entry) if isinstance(queue_entry, dict) else None,
    )


def _contract_payload_from_loader(
    *,
    index_root: Path,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> dict[str, Any] | None:
    try:
        payload = load_orca_contract_payload(
            index_root,
            target,
            queue_id=queue_id,
            run_id=run_id,
            reaction_dir=reaction_dir,
        )
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        return None
    if not any(
        normalize_text(payload.get(key))
        for key in ("run_id", "reaction_dir", "latest_known_path", "status", "queue_id")
    ):
        return None
    return {str(key): value for key, value in payload.items()}


def load_orca_contract_payload_impl(
    *,
    index_root: Path | None,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> dict[str, Any] | None:
    if index_root is None:
        return None
    return _contract_payload_from_loader(
        index_root=index_root,
        target=target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )


__all__ = [
    "load_orca_contract_payload_impl",
    "tracked_artifact_context_impl",
    "tracked_runtime_context_impl",
]
