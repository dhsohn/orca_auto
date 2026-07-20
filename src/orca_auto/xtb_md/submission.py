from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner
from orca_auto.core.commands.run_dir import (
    active_run_dir_pinned_target,
    assert_run_dir_publication_allowed,
    resolve_engine_job_dir,
)
from orca_auto.core.config.engines import load_xtb_md_config
from orca_auto.core.engine_process import atomic_write_confined_bytes
from orca_auto.core.queue import DuplicateQueueEntryError
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    finalize_queued_snapshot_intent,
    transition_snapshot_intent,
)
from orca_auto.core.queue.enqueue_publication import (
    EnqueuePublicationOutcome,
    EnqueuePublicationOutcomeUnknown,
    EnqueuePublicationSpec,
    run_enqueue_publication,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils.persistence import timestamped_token

from .command import build_xtb_md_command
from .generation import (
    cleanup_unowned_execution_generation,
    reserve_execution_generation,
    validate_xtb_md_generation,
)
from .input_builder import build_md_input
from .job_locations import index_root_for_path
from .limits import MAX_XTB_MD_OUTPUT_BYTES, manifest_limits_for_config
from .manifest import MANIFEST_FILE_NAME, XtbMdManifest, load_manifest, load_xyz_geometry
from .path_identity import (
    JOB_PATH_IDENTITY_KEY,
    capture_job_path_identity,
    validate_execution_snapshot_job_dir,
)
from .records import build_job_artifact, persist_job_artifact
from .version import probe_xtb_version

APP_NAME = "orca_auto_xtb_md"
ENGINE = "xtb_md"
TASK_KIND = "xtb_md"

logger = logging.getLogger(__name__)


def _snapshot_cleanup_job_dir(
    job_dir: Path,
    job_path_identity: Any,
) -> Path:
    pinned_target = active_run_dir_pinned_target()
    if pinned_target is None:
        return job_dir
    if not isinstance(job_path_identity, dict):
        raise ValueError("xTB-MD cleanup has no bound job directory identity")
    expected = job_path_identity.get("job_dir")
    if not isinstance(expected, dict):
        raise ValueError("xTB-MD cleanup has no bound job directory descriptor")
    expected_identity = (
        int(expected.get("device", -1)),
        int(expected.get("inode", -1)),
    )
    for candidate in (job_dir, pinned_target):
        try:
            candidate_stat = candidate.stat()
        except OSError:
            continue
        if (int(candidate_stat.st_dev), int(candidate_stat.st_ino)) == expected_identity:
            return candidate
    raise ValueError("xTB-MD cleanup target identity changed after submission")


def resolve_job_dir(cfg: Any, raw_job_dir: str) -> Path:
    return resolve_engine_job_dir(
        cfg,
        raw_job_dir,
        engine=ENGINE,
        workflow_error_message="xTB-MD standalone jobs must be inside runs_root",
    )


def _resource_request(cfg: Any, manifest: XtbMdManifest) -> dict[str, int]:
    return {
        "max_cores": int(
            manifest.resources.max_cores
            if manifest.resources.max_cores is not None
            else cfg.resources.max_cores_per_task
        ),
        "max_memory_gb": int(
            manifest.resources.max_memory_gb
            if manifest.resources.max_memory_gb is not None
            else cfg.resources.max_memory_gb_per_task
        ),
    }


def _reject_active_job_dir_duplicate(
    entries: Sequence[QueueEntry],
    proposed: QueueEntry,
) -> None:
    proposed_dir = str(proposed.metadata.get("job_dir") or "")
    for existing in entries:
        if existing.status not in {QueueStatus.PENDING, QueueStatus.RUNNING}:
            continue
        if existing.app_name != APP_NAME or existing.engine != ENGINE:
            continue
        if str(existing.metadata.get("job_dir") or "") == proposed_dir:
            raise DuplicateQueueEntryError(
                "An active xTB-MD generation already owns this job directory "
                f"(queue_id={existing.queue_id}, task_id={existing.task_id})"
            )


def _build_execution_snapshot(
    cfg: Any,
    job_dir: Path,
    manifest: XtbMdManifest,
    *,
    queue_root: Path,
    intent_token: str,
    job_path_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    resource_request = _resource_request(cfg, manifest)
    generation_name, execution_dir, generation_identity = reserve_execution_generation(
        job_dir,
        queue_root=queue_root,
        intent_token=intent_token,
    )
    cleanup_snapshot = {
        "version": 2,
        "job_dir": str(job_dir),
        JOB_PATH_IDENTITY_KEY: job_path_identity,
        "generation_name": generation_name,
        "execution_dir": str(execution_dir),
        "execution_dir_identity": {
            "device": generation_identity[0],
            "inode": generation_identity[1],
        },
        SNAPSHOT_INTENT_TOKEN_KEY: intent_token,
        SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(queue_root),
    }
    try:
        geometry_payload = read_stable_regular_file(
            manifest.input_xyz,
            require_single_link=True,
        )
        manifest_source = (job_dir / MANIFEST_FILE_NAME).resolve()
        manifest_payload = read_stable_regular_file(
            manifest_source,
            require_single_link=True,
        )
        md_input_payload = build_md_input(manifest).encode("utf-8")
        materialized_paths = {
            "geometry": execution_dir / manifest.input_xyz.name,
            "manifest": execution_dir / MANIFEST_FILE_NAME,
            "md_input": execution_dir / "md.inp",
        }
        for role, payload in (
            ("geometry", geometry_payload),
            ("manifest", manifest_payload),
            ("md_input", md_input_payload),
        ):
            atomic_write_confined_bytes(
                execution_dir,
                materialized_paths[role],
                payload,
                label=f"xTB-MD {role} snapshot",
                mode=0o400,
            )

        bound_manifest = load_manifest(
            execution_dir,
            limits=manifest_limits_for_config(cfg),
        )
        if bound_manifest.public_dict() != manifest.public_dict():
            raise ValueError("xTB-MD manifest changed while its generation was created")
        snapshot_symbols = load_xyz_geometry(
            materialized_paths["geometry"],
            max_atoms=manifest_limits_for_config(cfg).max_atoms,
        )
        if snapshot_symbols != manifest.atom_symbols:
            raise ValueError("xTB-MD input geometry changed while its generation was created")

        executable = engine_runner.resolve_configured_executable(
            cfg,
            path_attr="xtb_executable",
            executable_name="xtb",
            display_name="xTB",
        )
        executable_identity = engine_runner.executable_identity(executable)
        xtb_version = probe_xtb_version(executable)
        build_xtb_md_command(
            executable=executable,
            input_xyz=materialized_paths["geometry"],
            md_input=materialized_paths["md_input"],
            manifest=bound_manifest,
            max_cores=resource_request["max_cores"],
        )
        input_snapshots: dict[str, dict[str, Any]] = {}
        for role, path in materialized_paths.items():
            identity = engine_runner.confined_output_identity(execution_dir, path)
            input_snapshots[role] = {
                "role": role,
                "source_path": str(manifest.input_xyz if role == "geometry" else manifest_source),
                "snapshot_path": identity["path"],
                "sha256": identity["sha256"],
                "size_bytes": identity["size_bytes"],
            }
        snapshot = {
            **cleanup_snapshot,
            "attempt_limit": 1,
            "retry_supported": False,
            "resume_supported": False,
            "manifest": bound_manifest.public_dict(),
            "derived_budget": {
                "atom_count": bound_manifest.atom_count,
                "expected_steps": bound_manifest.expected_steps,
                "expected_frames": bound_manifest.expected_frames,
                "atom_steps": bound_manifest.atom_steps,
                "walltime_seconds": bound_manifest.walltime_seconds,
                "max_output_bytes": MAX_XTB_MD_OUTPUT_BYTES,
            },
            "input_snapshots": input_snapshots,
            "selected_input_xyz": str(materialized_paths["geometry"].resolve()),
            "manifest_path": str(materialized_paths["manifest"].resolve()),
            "md_input_path": str(materialized_paths["md_input"].resolve()),
            "resource_request": resource_request,
            "executable_identity": executable_identity,
            "xtb_version": xtb_version,
            "runtime_identity": engine_runner.engine_runtime_identity(job_dir),
        }
        validate_xtb_md_generation(cfg.runtime.allowed_root, snapshot)
        return snapshot, resource_request
    except BaseException:
        cleanup_unowned_execution_generation(
            _snapshot_cleanup_job_dir(job_dir, job_path_identity),
            cleanup_snapshot,
        )
        raise


def _build_submission(
    cfg: Any,
    job_dir: Path,
) -> tuple[dict[str, Any], str, Path]:
    job_path_identity = capture_job_path_identity(job_dir, cfg.runtime.allowed_root)
    manifest = load_manifest(job_dir, limits=manifest_limits_for_config(cfg))
    queue_root = index_root_for_path(cfg, job_dir)
    task_id = timestamped_token("xtbmd", token_bytes=16)
    intent_token = timestamped_token("snapshot_intent", token_bytes=16)
    execution_snapshot: dict[str, Any] | None = None
    try:
        execution_snapshot, resource_request = _build_execution_snapshot(
            cfg,
            job_dir,
            manifest,
            queue_root=queue_root,
            intent_token=intent_token,
            job_path_identity=job_path_identity,
        )
        molecule_key = manifest.input_xyz.stem
        metadata = {
            "job_dir": str(job_dir),
            "execution_dir": execution_snapshot["execution_dir"],
            "manifest_path": execution_snapshot["manifest_path"],
            "selected_input_xyz": execution_snapshot["selected_input_xyz"],
            "ensemble": manifest.ensemble,
            "molecule_key": molecule_key,
            "resource_request": resource_request,
            "resource_actual": dict(resource_request),
            "execution_snapshot": execution_snapshot,
            "submitted_at": now_utc_iso(),
            "retry_supported": False,
            "resume_supported": False,
        }
        return metadata, task_id, queue_root
    except BaseException:
        path_identity_valid = False
        cleanup_job_dir = job_dir
        try:
            cleanup_job_dir = _snapshot_cleanup_job_dir(job_dir, job_path_identity)
            if cleanup_job_dir != job_dir:
                path_identity_valid = True
            elif execution_snapshot is not None:
                validate_execution_snapshot_job_dir(cfg.runtime.allowed_root, execution_snapshot)
                path_identity_valid = True
            else:
                current_identity = capture_job_path_identity(job_dir, cfg.runtime.allowed_root)
                path_identity_valid = current_identity == job_path_identity
        except Exception:  # noqa: BLE001
            path_identity_valid = False
        if execution_snapshot is not None and path_identity_valid:
            cleanup_unowned_execution_generation(cleanup_job_dir, execution_snapshot)
        raise


def publish_queued_record(cfg: Any, entry: QueueEntry) -> None:
    """Publish the queued job artifact for one committed queue row."""
    payload = build_job_artifact(
        entry,
        status="queued",
        reason="queued",
        exit_code=None,
    )
    persist_job_artifact(cfg, entry, payload)


def _enqueue_submission(
    cfg: Any,
    job_dir: Path,
    *,
    priority: int,
) -> EnqueuePublicationOutcome:
    assert_run_dir_publication_allowed("xTB-MD target mutation preflight")
    metadata, task_id, queue_root = _build_submission(cfg, job_dir)
    intent_token = str(metadata["execution_snapshot"][SNAPSHOT_INTENT_TOKEN_KEY])

    def cleanup_submission_snapshot() -> None:
        cleanup_unowned_execution_generation(
            _snapshot_cleanup_job_dir(
                job_dir,
                metadata["execution_snapshot"].get(JOB_PATH_IDENTITY_KEY),
            ),
            metadata["execution_snapshot"],
        )

    try:
        transition_snapshot_intent(
            queue_root,
            intent_token,
            target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
            expected_states={SNAPSHOT_INTENT_STATE_CREATING},
        )
    except BaseException:
        try:
            cleanup_submission_snapshot()
        except BaseException:  # noqa: BLE001 - preserve the transition-origin failure
            logger.exception("Failed to clean the xTB-MD submission snapshot after an intent error")
        raise

    spec = EnqueuePublicationSpec(
        queue_root=queue_root,
        app_name=APP_NAME,
        task_id=task_id,
        task_kind=TASK_KIND,
        engine=ENGINE,
        priority=priority,
        metadata=metadata,
        label="xTB-MD",
        publish=lambda entry: publish_queued_record(cfg, entry),
        duplicate_policy=_reject_active_job_dir_duplicate,
        before_commit_fn=lambda: assert_run_dir_publication_allowed(
            "xTB-MD durable queue pre-commit"
        ),
        after_commit_fn=lambda: assert_run_dir_publication_allowed(
            "xTB-MD durable queue post-commit"
        ),
        finalize_intent=lambda entry: finalize_queued_snapshot_intent(queue_root, entry),
        on_compensated_failure=cleanup_submission_snapshot,
    )
    return run_enqueue_publication(spec)


def submit_job_dir(
    *,
    job_dir: str,
    priority: int,
    config_path: str | None,
) -> dict[str, Any]:
    try:
        cfg = load_xtb_md_config(config_path)
        resolved_job_dir = resolve_job_dir(cfg, job_dir)
        outcome = _enqueue_submission(
            cfg,
            resolved_job_dir,
            priority=priority,
        )
    except EnqueuePublicationOutcomeUnknown as exc:
        return {
            "status": "failed",
            "reason": "queue_enqueue_outcome_unknown",
            "stderr": f"{exc.__class__.__name__}: {exc}",
            "job_dir": str(job_dir),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "reason": "submission_failed",
            "stderr": f"{exc.__class__.__name__}: {exc}",
            "job_dir": str(job_dir),
        }
    entry = outcome.entry
    if outcome.cancelled or entry.status == QueueStatus.CANCELLED:
        return {
            "status": "cancelled",
            "reason": "submission_cancelled",
            "job_dir": str(resolved_job_dir),
            "job_id": entry.task_id,
            "queue_id": entry.queue_id,
        }
    payload = {
        "status": "queued",
        "job_dir": str(resolved_job_dir),
        "job_id": entry.task_id,
        "queue_id": entry.queue_id,
        "priority": entry.priority,
        "ensemble": str(entry.metadata.get("ensemble") or ""),
    }
    if not outcome.published:
        # The row is durably queued but its queued record is not published
        # yet; the worker's pre-claim repair pass reconciles it before the
        # row can run, so the submission itself succeeded.
        payload["publication"] = "deferred"
    if outcome.warnings:
        payload["warnings"] = list(outcome.warnings)
    return payload


__all__ = [
    "APP_NAME",
    "ENGINE",
    "TASK_KIND",
    "resolve_job_dir",
    "submit_job_dir",
]
