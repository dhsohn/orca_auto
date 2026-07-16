from __future__ import annotations

import json
import logging
import os
import secrets
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
from orca_auto.core.queue import (
    DuplicateQueueEntryError,
    enqueue,
    list_queue,
    mark_failed,
    update_metadata,
)
from orca_auto.core.queue.engine.input_snapshot import (
    cleanup_unowned_input_snapshot_namespace,
    reserve_input_snapshot_namespace,
    snapshot_input_file,
    snapshot_input_payload,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    create_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
    finalize_queued_snapshot_intent,
    transition_snapshot_intent,
)
from orca_auto.core.queue.generation import queue_entries_same_generation
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.store import QueueAfterCommitError
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils.persistence import timestamped_token

from .command import build_xtb_md_command
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


class XtbMdSubmissionError(RuntimeError):
    def __init__(self, message: str, *, entry: QueueEntry | None = None):
        super().__init__(message)
        self.entry = entry


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
    snapshot_namespace: str,
    job_path_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    resource_request = _resource_request(cfg, manifest)
    geometry_descriptor = snapshot_input_file(
        job_dir,
        manifest.input_xyz,
        role="geometry",
        namespace=snapshot_namespace,
    )
    snapshot_symbols = load_xyz_geometry(
        geometry_descriptor["snapshot_path"],
        max_atoms=manifest_limits_for_config(cfg).max_atoms,
    )
    if snapshot_symbols != manifest.atom_symbols:
        raise ValueError("xTB-MD input geometry changed while its snapshot was created")

    canonical_manifest = manifest.public_dict()
    manifest_payload = json.dumps(
        canonical_manifest,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest_descriptor = snapshot_input_payload(
        job_dir,
        manifest_payload,
        role="manifest",
        suffix=".json",
        source_path=job_dir / MANIFEST_FILE_NAME,
        namespace=snapshot_namespace,
    )
    md_input_payload = build_md_input(manifest).encode("utf-8")
    md_input_descriptor = snapshot_input_payload(
        job_dir,
        md_input_payload,
        role="md_input",
        suffix=".inp",
        source_path=job_dir / MANIFEST_FILE_NAME,
        namespace=snapshot_namespace,
    )

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
        input_xyz=geometry_descriptor["snapshot_path"],
        md_input=md_input_descriptor["snapshot_path"],
        manifest=manifest,
        max_cores=resource_request["max_cores"],
    )
    snapshot = {
        "version": 1,
        "attempt_limit": 1,
        "retry_supported": False,
        "resume_supported": False,
        "job_dir": str(job_dir),
        JOB_PATH_IDENTITY_KEY: job_path_identity,
        "snapshot_namespace": snapshot_namespace,
        SNAPSHOT_INTENT_TOKEN_KEY: snapshot_namespace,
        SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(queue_root),
        "manifest": canonical_manifest,
        "derived_budget": {
            "atom_count": manifest.atom_count,
            "expected_steps": manifest.expected_steps,
            "expected_frames": manifest.expected_frames,
            "atom_steps": manifest.atom_steps,
            "walltime_seconds": manifest.walltime_seconds,
            "max_output_bytes": MAX_XTB_MD_OUTPUT_BYTES,
        },
        "input_snapshots": {
            "geometry": geometry_descriptor,
            "manifest": manifest_descriptor,
            "md_input": md_input_descriptor,
        },
        "selected_input_xyz": geometry_descriptor["snapshot_path"],
        "manifest_path": manifest_descriptor["snapshot_path"],
        "md_input_path": md_input_descriptor["snapshot_path"],
        "resource_request": resource_request,
        "executable_identity": executable_identity,
        "xtb_version": xtb_version,
        "runtime_identity": engine_runner.engine_runtime_identity(job_dir),
    }
    return snapshot, resource_request


def _build_submission(
    cfg: Any,
    job_dir: Path,
) -> tuple[dict[str, Any], str, Path]:
    job_path_identity = capture_job_path_identity(job_dir, cfg.runtime.allowed_root)
    manifest = load_manifest(job_dir, limits=manifest_limits_for_config(cfg))
    queue_root = index_root_for_path(cfg, job_dir)
    task_id = timestamped_token("xtbmd", token_bytes=16)
    snapshot_namespace = f"snapshot-{secrets.token_hex(16)}"
    generation_dir = job_dir / ".orca_auto_input_snapshots" / snapshot_namespace
    intent_created = False
    namespace_created = False
    execution_snapshot: dict[str, Any] | None = None
    try:
        create_snapshot_intent(
            queue_root,
            token=snapshot_namespace,
            kind="input_snapshot_namespace",
            generation_paths=[generation_dir],
        )
        intent_created = True
        reserve_input_snapshot_namespace(job_dir, snapshot_namespace)
        namespace_created = True
        execution_snapshot, resource_request = _build_execution_snapshot(
            cfg,
            job_dir,
            manifest,
            queue_root=queue_root,
            snapshot_namespace=snapshot_namespace,
            job_path_identity=job_path_identity,
        )
        validate_execution_snapshot_job_dir(cfg.runtime.allowed_root, execution_snapshot)
        molecule_key = manifest.input_xyz.stem
        metadata = {
            "job_dir": str(job_dir),
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
        if namespace_created and path_identity_valid:
            cleanup_unowned_input_snapshot_namespace(cleanup_job_dir, snapshot_namespace)
        if intent_created:
            discard_snapshot_intent_if_generations_absent(queue_root, snapshot_namespace)
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


def _fence_uncompensated_xtb_md_enqueue(
    queue_root: Path,
    *,
    error: QueueAfterCommitError,
    publication_token: str,
    task_id: str,
) -> QueueEntry | None:
    """Terminally fence one exact provisional row without publishing it."""

    entry = error.provisional_result
    metadata = entry.metadata if isinstance(entry, QueueEntry) else {}
    if (
        not isinstance(entry, QueueEntry)
        or entry.task_id != task_id
        or str(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY) or "") != publication_token
    ):
        logger.error("Cannot identify the provisional xTB-MD row after compensation failure")
        return None
    try:
        fenced = mark_failed(
            queue_root,
            entry.queue_id,
            error=(
                "queue_after_commit_guard_failed:"
                f"compensation={error.compensation_outcome}:"
                f"{error.after_commit_error.__class__.__name__}:"
                f"{error.after_commit_error}"
            ),
            metadata_update=queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_ABORTED,
                token=publication_token,
                owner_pid=0,
            ),
            expected_entry=entry,
            expected_task_id=task_id,
        )
    except BaseException:  # noqa: BLE001 - preserve the guard-origin failure path
        logger.exception(
            "Failed to terminally fence the provisional xTB-MD row: queue_id=%s",
            entry.queue_id,
        )
        return None
    if fenced is None:
        logger.error(
            "Provisional xTB-MD row was not visible for terminal fencing: queue_id=%s",
            entry.queue_id,
        )
    return fenced


def _enqueue_submission(
    cfg: Any,
    job_dir: Path,
    *,
    priority: int,
) -> QueueEntry:
    assert_run_dir_publication_allowed("xTB-MD target mutation preflight")
    metadata, task_id, queue_root = _build_submission(cfg, job_dir)
    publication_token = timestamped_token("record_sync", token_bytes=16)
    metadata.update(
        queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING,
            token=publication_token,
            owner_pid=os.getpid(),
        )
    )
    snapshot_namespace = str(metadata["execution_snapshot"][SNAPSHOT_INTENT_TOKEN_KEY])
    entry: QueueEntry | None = None
    try:
        transition_snapshot_intent(
            queue_root,
            snapshot_namespace,
            target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
            expected_states={SNAPSHOT_INTENT_STATE_CREATING},
        )
        entry = enqueue(
            queue_root,
            app_name=APP_NAME,
            task_id=task_id,
            task_kind=TASK_KIND,
            engine=ENGINE,
            priority=priority,
            metadata=metadata,
            duplicate_policy=_reject_active_job_dir_duplicate,
            before_commit_fn=lambda: assert_run_dir_publication_allowed(
                "xTB-MD durable queue pre-commit"
            ),
            after_commit_fn=lambda: assert_run_dir_publication_allowed(
                "xTB-MD durable queue post-commit"
            ),
        )
    except QueueAfterCommitError as exc:
        if exc.compensation_succeeded:
            try:
                cleanup_unowned_input_snapshot_namespace(
                    _snapshot_cleanup_job_dir(
                        job_dir,
                        metadata["execution_snapshot"].get(JOB_PATH_IDENTITY_KEY),
                    ),
                    snapshot_namespace,
                )
                discard_snapshot_intent_if_generations_absent(
                    queue_root,
                    snapshot_namespace,
                )
            except BaseException:  # noqa: BLE001 - preserve the guard-origin failure path
                logger.exception(
                    "Failed to clean the compensated xTB-MD submission snapshot; retaining it"
                )
        else:
            _fence_uncompensated_xtb_md_enqueue(
                queue_root,
                error=exc,
                publication_token=publication_token,
                task_id=task_id,
            )
        raise
    except BaseException as exc:
        recovered = [
            candidate
            for candidate in list_queue(queue_root)
            if candidate.task_id == task_id
            and candidate.app_name == APP_NAME
            and candidate.engine == ENGINE
            and str(candidate.metadata.get("job_dir") or "") == str(job_dir)
            and str(candidate.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY) or "") == publication_token
        ]
        if len(recovered) != 1:
            cleanup_unowned_input_snapshot_namespace(
                _snapshot_cleanup_job_dir(
                    job_dir,
                    metadata["execution_snapshot"].get(JOB_PATH_IDENTITY_KEY),
                ),
                snapshot_namespace,
            )
            discard_snapshot_intent_if_generations_absent(queue_root, snapshot_namespace)
            raise
        if not isinstance(exc, Exception):
            raise
        entry = recovered[0]

    assert entry is not None
    try:
        finalize_queued_snapshot_intent(queue_root, entry)
        with queue_record_publication_lock(queue_root, entry.queue_id):
            owned = [
                candidate
                for candidate in list_queue(queue_root)
                if candidate.queue_id == entry.queue_id
                and candidate.task_id == entry.task_id
                and queue_entries_same_generation(candidate, entry)
            ]
            if len(owned) != 1:
                raise RuntimeError("xTB-MD queued-record publication ownership was revoked")
            current = owned[0]
            if current.status == QueueStatus.CANCELLED:
                # Pending cancellation publishes its generation-bound terminal
                # artifacts before committing the queue transition. A late
                # publisher must not write again here because a replacement
                # generation may already be publishing in the shared job dir.
                entry = current
                return entry
            if (
                current.status != QueueStatus.PENDING
                or queue_record_sync_state(current) != QUEUE_RECORD_SYNC_PREPARING
                or str(current.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY) or "") != publication_token
            ):
                raise RuntimeError("xTB-MD queued-record publication ownership was revoked")
            entry = current
            publish_queued_record(cfg, entry)
            updated = update_metadata(
                queue_root,
                entry.queue_id,
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE,
                    token=publication_token,
                    owner_pid=0,
                ),
                expected_entry=entry,
                expected_task_id=entry.task_id,
            )
            if updated is None:
                raise RuntimeError("xTB-MD queue publication ownership was revoked")
            entry = updated
            if entry.status != QueueStatus.PENDING:
                raise RuntimeError(
                    "xTB-MD queue entry changed state during queued-record publication"
                )
    except BaseException as exc:
        failure_metadata = {
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_ABORTED,
        }
        mark_failed(
            queue_root,
            entry.queue_id,
            error=f"queued_record_publication_failed:{exc}",
            metadata_update=failure_metadata,
            expected_entry=entry,
            expected_task_id=entry.task_id,
        )
        raise XtbMdSubmissionError(
            f"xTB-MD queue publication failed after enqueue: {exc}",
            entry=entry,
        ) from exc
    return entry


def submit_job_dir(
    *,
    job_dir: str,
    priority: int,
    config_path: str | None,
) -> dict[str, Any]:
    try:
        cfg = load_xtb_md_config(config_path)
        resolved_job_dir = resolve_job_dir(cfg, job_dir)
        entry = _enqueue_submission(
            cfg,
            resolved_job_dir,
            priority=priority,
        )
    except QueueAfterCommitError as exc:
        return {
            "status": "failed",
            "reason": (
                "submission_failed"
                if exc.compensation_succeeded
                else "queue_enqueue_outcome_unknown"
            ),
            "stderr": f"{exc.__class__.__name__}: {exc}",
            "job_dir": str(job_dir),
        }
    except XtbMdSubmissionError as exc:
        failed_entry = exc.entry
        return {
            "status": "failed",
            "reason": "submission_publication_failed",
            "stderr": str(exc),
            "queue_id": str(getattr(failed_entry, "queue_id", "") or ""),
            "job_id": str(getattr(failed_entry, "task_id", "") or ""),
            "job_dir": str(job_dir),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "reason": "submission_failed",
            "stderr": f"{exc.__class__.__name__}: {exc}",
            "job_dir": str(job_dir),
        }
    if entry.status == QueueStatus.CANCELLED:
        return {
            "status": "cancelled",
            "reason": "submission_cancelled",
            "job_dir": str(resolved_job_dir),
            "job_id": entry.task_id,
            "queue_id": entry.queue_id,
        }
    return {
        "status": "queued",
        "job_dir": str(resolved_job_dir),
        "job_id": entry.task_id,
        "queue_id": entry.queue_id,
        "priority": entry.priority,
        "ensemble": str(entry.metadata.get("ensemble") or ""),
    }


__all__ = [
    "APP_NAME",
    "ENGINE",
    "TASK_KIND",
    "XtbMdSubmissionError",
    "resolve_job_dir",
    "submit_job_dir",
]
