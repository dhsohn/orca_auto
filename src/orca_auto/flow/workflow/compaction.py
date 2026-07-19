from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from orca_auto.core.admission import admission_lock, list_all_slots
from orca_auto.core.config.files import default_shared_admission_root
from orca_auto.core.indexing.store import (
    JOB_LOCATION_INDEX_FILE_NAME,
    JOB_LOCATION_INDEX_LOCK_NAME,
)
from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    is_workflow_workspace_location,
    validate_workflow_workspace_identity,
    workflow_stage_dirnames_for_engine,
)
from orca_auto.core.queue.engine.artifacts import terminal_state_is_consistent
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.store import (
    QUEUE_FILE_NAME,
    entry_from_dict,
    queue_lock,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.lock import tmpfs_file_lock
from orca_auto.core.utils.persistence import parse_iso_utc
from orca_auto.flow.registry.store import (
    WORKFLOW_REGISTRY_CLEARED_FILE_NAME,
    WORKFLOW_REGISTRY_FILE_NAME,
    WORKFLOW_REGISTRY_LOCK_NAME,
)

from .status import workflow_status_is_terminal
from .store import acquire_workflow_lock
from .summary import workflow_has_active_downstream

_REPORT_LEAVES = ("job_report.json", "job_report.md")
_SUPPORTED_ENGINES = frozenset({"xtb", "crest"})
_EXPECTED_APPS = {"xtb": "orca_auto_xtb", "crest": "orca_auto_crest"}
_SNAPSHOT_INTENT_DIR_NAME = ".orca_auto_snapshot_intents"
_SNAPSHOT_MUTATION_LOCK_NAME = ".orca_auto_snapshot_intents.mutation.lock"
_MAX_CONTROL_BYTES = 8 * 1024 * 1024
_MAX_CONTROL_ROWS = 65_536
_MAX_JOB_REFS = 4_096
_MAX_CANDIDATES = 128
_MAX_REPORT_BYTES = 16 * 1024 * 1024
_PRESERVED = (
    "workflow, registry, journal, queue, index, admission, and snapshot state",
    "canonical job_state.json",
    "scientific outputs, logs, geometries, provenance, SI, data, and HTML",
    "ORCA and standalone xTB-MD reports",
    "all lock files",
)


@dataclass(frozen=True)
class CompactionArtifact:
    relative_path: str
    size: int
    engine: str
    stage_id: str
    job_id: str


@dataclass(frozen=True)
class WorkflowCompactionResult:
    workflow_id: str
    workspace_dir: str
    eligible: bool
    blocked: bool
    applied: bool
    would_remove: tuple[CompactionArtifact, ...]
    removed: tuple[str, ...]
    removed_bytes: int
    preserved: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _JobRef:
    engine: str
    stage_id: str
    relative_dir: PurePosixPath
    queue_root: Path
    queue_id: str
    expected_job_id: str


@dataclass(frozen=True)
class _VerifiedJob:
    ref: _JobRef
    job_id: str


@dataclass(frozen=True)
class _Candidate:
    artifact: CompactionArtifact
    device: int
    inode: int


class _Blocked(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _blocked(message: str) -> NoReturn:
    raise _Blocked(message)


def _owned_directory(path: Path, *, label: str) -> Path:
    absolute = path.expanduser().absolute()
    try:
        details = absolute.lstat()
    except FileNotFoundError:
        raise ValueError(f"{label} does not exist: {absolute}") from None
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected: {absolute}") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != os.geteuid()
        or absolute.resolve() != absolute
    ):
        raise ValueError(
            f"{label} must be an owned directory without symlink traversal: {absolute}"
        )
    return absolute


def _read_json_file(
    path: Path,
    *,
    label: str,
    missing: Any,
    limit: int = _MAX_CONTROL_BYTES,
) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return missing
    except OSError as exc:
        _blocked(f"{label} cannot be opened safely: {exc}")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size > limit
        ):
            _blocked(f"{label} must be a bounded, owned, single-link regular file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            named = path.lstat()
        except OSError as exc:
            _blocked(f"{label} changed while being read: {exc}")
        if (
            len(payload) > limit
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
        ):
            _blocked(f"{label} changed while being read")
    finally:
        os.close(descriptor)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _blocked(f"{label} is invalid JSON: {exc}")


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    value = _read_json_file(path, label=label, missing=None)
    if not isinstance(value, dict):
        _blocked(f"{label} must contain a JSON object")
    return value


def _json_rows(path: Path, *, label: str) -> list[dict[str, Any]]:
    value = _read_json_file(path, label=label, missing=[])
    if (
        not isinstance(value, list)
        or len(value) > _MAX_CONTROL_ROWS
        or any(not isinstance(row, dict) for row in value)
    ):
        _blocked(f"{label} must contain a bounded list of objects")
    return value


def _pending_flags(mapping: Mapping[str, Any], *, label: str) -> None:
    for key in (
        "final_child_sync_pending",
        "si_publish_pending",
        "si_publish_blocked",
        "repair_pending",
        "replay_pending",
        "admission_pending",
        "snapshot_pending",
        "cancel_requested",
        "cancellation_pending",
    ):
        if mapping.get(key) not in (None, False, ""):
            _blocked(f"{label} still has {key}")


def _relative_job_dir(
    raw_value: Any,
    *,
    workspace: Path,
    engine: str,
    stage_id: str,
) -> PurePosixPath:
    raw = _text(raw_value)
    candidate = Path(raw)
    if (
        not raw
        or not candidate.is_absolute()
        or any(part in {".", ".."} for part in candidate.parts)
    ):
        _blocked(f"{engine} stage {stage_id} has a non-canonical job_dir")
    if candidate.resolve() != candidate:
        _blocked(f"{engine} stage {stage_id} job_dir traverses a symlink")
    try:
        relative = candidate.relative_to(workspace)
    except ValueError:
        _blocked(f"{engine} stage {stage_id} job_dir escapes the workflow workspace")
    if (
        len(relative.parts) < 2
        or relative.parts[0] not in workflow_stage_dirnames_for_engine(engine)
        or relative.parts[1] != stage_id
    ):
        _blocked(f"{engine} stage {stage_id} job_dir is outside its exact stage directory")
    return PurePosixPath(*relative.parts)


def _derive_job_refs(
    payload: dict[str, Any],
    *,
    workspace: Path,
) -> tuple[str, tuple[_JobRef, ...]]:
    workflow_id = _text(payload.get("workflow_id"))
    try:
        persisted_id = validate_workflow_workspace_identity(workspace, workflow_id)
    except ValueError as exc:
        _blocked(f"workflow identity is corrupt: {exc}")
    if _text(payload.get("status")).lower() != "completed":
        _blocked("workflow status is not exactly completed")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        _blocked("workflow metadata is missing or corrupt")
    _pending_flags(metadata, label="workflow")
    if parse_iso_utc(metadata.get("final_child_sync_completed_at")) is None:
        _blocked("workflow final child synchronization is not durably complete")
    publish_generation = _text(metadata.get("si_publish_generation"))
    published_generation = _text(metadata.get("si_published_generation"))
    if publish_generation != published_generation:
        _blocked("workflow SI publication is not complete")
    if workflow_has_active_downstream(payload):
        _blocked("workflow has active downstream work")

    stages = payload.get("stages")
    if not isinstance(stages, list) or not stages:
        _blocked("workflow has no canonical stages")
    refs: list[_JobRef] = []
    seen_stages: set[str] = set()
    for raw_stage in stages:
        if not isinstance(raw_stage, dict):
            _blocked("workflow contains a corrupt stage row")
        stage_id = _text(raw_stage.get("stage_id"))
        if not stage_id or stage_id in seen_stages:
            _blocked("workflow stage identity is missing or duplicated")
        seen_stages.add(stage_id)
        task = raw_stage.get("task")
        if not isinstance(task, dict):
            _blocked(f"stage {stage_id} has no canonical task")
        stage_status = _text(raw_stage.get("status")).lower()
        task_status = _text(task.get("status")).lower()
        if not workflow_status_is_terminal(stage_status) or not workflow_status_is_terminal(
            task_status
        ):
            _blocked(f"stage {stage_id} and its task are not terminal")
        engine = _text(task.get("engine")).lower()
        if engine not in _SUPPORTED_ENGINES:
            continue
        if stage_status != "completed" or task_status != "completed":
            continue
        stage_metadata = raw_stage.get("metadata")
        task_metadata = task.get("metadata")
        task_payload = task.get("payload")
        enqueue_payload = task.get("enqueue_payload")
        submission = task.get("submission_result")
        if not all(
            isinstance(value, dict)
            for value in (stage_metadata, task_metadata, task_payload, enqueue_payload, submission)
        ):
            _blocked(f"{engine} stage {stage_id} lacks canonical task metadata")
        assert isinstance(stage_metadata, dict)
        assert isinstance(task_metadata, dict)
        assert isinstance(task_payload, dict)
        assert isinstance(enqueue_payload, dict)
        assert isinstance(submission, dict)
        _pending_flags(stage_metadata, label=f"stage {stage_id}")
        _pending_flags(task_metadata, label=f"task {stage_id}")

        current_dir = _relative_job_dir(
            task_payload.get("job_dir"),
            workspace=workspace,
            engine=engine,
            stage_id=stage_id,
        )
        current_path = str(workspace.joinpath(*current_dir.parts))
        current_queue = _text(submission.get("queue_id"))
        current_job = _text(stage_metadata.get("child_job_id"))
        if (
            _text(enqueue_payload.get("job_dir")) != current_path
            or _text(submission.get("job_dir")) != current_path
            or not current_queue
            or _text(stage_metadata.get("queue_id")) != current_queue
            or not current_job
        ):
            _blocked(f"{engine} stage {stage_id} current job identity mismatch")

        attempts = stage_metadata.get("xtb_attempts") if engine == "xtb" else None
        if attempts is not None and not isinstance(attempts, list):
            _blocked(f"xTB stage {stage_id} attempt metadata is corrupt")
        if engine == "xtb" and attempts:
            active_number = stage_metadata.get("xtb_active_attempt_number")
            if (
                type(active_number) is not int
                or task_payload.get("xtb_active_attempt_number") != active_number
            ):
                _blocked(f"xTB stage {stage_id} active attempt number is not canonical")
            active_matches = 0
            seen_attempts: set[int] = set()
            for attempt in attempts:
                if not isinstance(attempt, dict) or type(attempt.get("attempt_number")) is not int:
                    _blocked(f"xTB stage {stage_id} has a corrupt attempt row")
                attempt_number = int(attempt["attempt_number"])
                if attempt_number < 0 or attempt_number in seen_attempts:
                    _blocked(f"xTB stage {stage_id} has duplicate attempt metadata")
                seen_attempts.add(attempt_number)
                relative = _relative_job_dir(
                    attempt.get("job_dir"),
                    workspace=workspace,
                    engine=engine,
                    stage_id=stage_id,
                )
                queue_id = _text(attempt.get("queue_id"))
                job_id = _text(attempt.get("job_id"))
                if not queue_id or not job_id:
                    _blocked(f"xTB stage {stage_id} attempt lacks terminal identity")
                if attempt_number == active_number:
                    active_matches += int(
                        relative == current_dir
                        and queue_id == current_queue
                        and job_id == current_job
                    )
                refs.append(
                    _JobRef(
                        engine=engine,
                        stage_id=stage_id,
                        relative_dir=relative,
                        queue_root=workspace / relative.parts[0],
                        queue_id=queue_id,
                        expected_job_id=job_id,
                    )
                )
            if active_matches != 1:
                _blocked(f"xTB stage {stage_id} active attempt identity mismatch")
        else:
            refs.append(
                _JobRef(
                    engine=engine,
                    stage_id=stage_id,
                    relative_dir=current_dir,
                    queue_root=workspace / current_dir.parts[0],
                    queue_id=current_queue,
                    expected_job_id=current_job,
                )
            )

    unique: dict[tuple[str, str], _JobRef] = {}
    for ref in refs:
        key = (ref.queue_id, str(ref.relative_dir))
        if key in unique:
            _blocked("workflow job identity is duplicated")
        unique[key] = ref
    if len(unique) > _MAX_JOB_REFS:
        _blocked("workflow job reference count exceeds the safety bound")
    return persisted_id, tuple(unique.values())


def _registry_is_exact(root: Path, *, workflow_id: str, workspace: Path) -> None:
    records = _json_rows(root / WORKFLOW_REGISTRY_FILE_NAME, label="workflow registry")
    markers = _json_rows(
        root / WORKFLOW_REGISTRY_CLEARED_FILE_NAME,
        label="workflow cleared markers",
    )
    expected_file = str(workspace / WORKFLOW_FILE_NAME)

    def exact(row: dict[str, Any]) -> bool:
        return (
            _text(row.get("workflow_id")) == workflow_id
            and _text(row.get("status")).lower() == "completed"
            and _text(row.get("workspace_dir")) == str(workspace)
            and _text(row.get("workflow_file")) == expected_file
        )

    exact_records = [row for row in records if exact(row)]
    exact_markers = [row for row in markers if exact(row)]
    conflicting = [
        row
        for row in (*records, *markers)
        if (
            _text(row.get("workflow_id")) == workflow_id
            or _text(row.get("workspace_dir")) == str(workspace)
        )
        and not exact(row)
    ]
    if conflicting or len(exact_records) + len(exact_markers) != 1:
        _blocked("workflow lacks one exact completed registry row or cleared marker")
    if exact_records:
        metadata = exact_records[0].get("metadata")
        if not isinstance(metadata, dict):
            _blocked("workflow registry metadata is corrupt")
        _pending_flags(metadata, label="workflow registry")


def _queue_entries(queue_root: Path) -> list[QueueEntry]:
    rows = _json_rows(queue_root / QUEUE_FILE_NAME, label=f"queue state {queue_root}")
    entries: list[QueueEntry] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        try:
            entry = entry_from_dict(row)
        except (TypeError, ValueError, RuntimeError) as exc:
            _blocked(f"queue state {queue_root} entry {index} is invalid: {exc}")
        if not entry.queue_id or entry.queue_id in seen:
            _blocked(f"queue state {queue_root} has a blank or duplicated queue id")
        seen.add(entry.queue_id)
        entries.append(entry)
    return entries


def _index_records(queue_root: Path) -> list[dict[str, Any]]:
    return _json_rows(
        queue_root / JOB_LOCATION_INDEX_FILE_NAME,
        label=f"job location index {queue_root}",
    )


def _verify_index_fallback(
    records: Sequence[dict[str, Any]],
    *,
    ref: _JobRef,
    job_id: str,
    job_dir: Path,
) -> None:
    expected_app = _EXPECTED_APPS[ref.engine]

    def exact(row: dict[str, Any]) -> bool:
        return (
            _text(row.get("job_id")) == job_id
            and _text(row.get("app_name")) == expected_app
            and _text(row.get("status")).lower() == "completed"
            and _text(row.get("original_run_dir")) == str(job_dir)
            and _text(row.get("latest_known_path")) == str(job_dir)
        )

    matches = [row for row in records if exact(row)]
    if any(
        not exact(row)
        and str(job_dir)
        in {
            _text(row.get("original_run_dir")),
            _text(row.get("latest_known_path")),
        }
        for row in records
    ):
        _blocked(f"{ref.engine} job directory is shared by another indexed generation")
    if len(matches) != 1:
        _blocked(f"{ref.engine} job {job_id} lacks exact queue or index evidence")


def _verify_jobs(workspace: Path, refs: Sequence[_JobRef]) -> tuple[_VerifiedJob, ...]:
    by_root: dict[Path, tuple[list[QueueEntry], list[dict[str, Any]]]] = {}
    for queue_root in sorted({ref.queue_root for ref in refs}, key=str):
        _owned_directory(queue_root, label=f"queue root {queue_root}")
        by_root[queue_root] = (_queue_entries(queue_root), _index_records(queue_root))

    verified: list[_VerifiedJob] = []
    for ref in refs:
        entries, index = by_root[ref.queue_root]
        job_dir = workspace.joinpath(*ref.relative_dir.parts)
        _owned_directory(job_dir, label=f"{ref.engine} job directory")
        state = _json_object(job_dir / "job_state.json", label=f"state for {ref.relative_dir}")
        job = state.get("job")
        recovery = state.get("recovery")
        process = state.get("process")
        if not isinstance(job, dict):
            _blocked(f"{ref.engine} job state has no identity")
        job_id = _text(job.get("id"))
        if (
            state.get("schema_version") != 1
            or _text(state.get("engine")).lower() != ref.engine
            or job_id != ref.expected_job_id
            or _text(job.get("task_id")) != job_id
            or _text(job.get("queue_id")) != ref.queue_id
            or _text(job.get("app_name")) != _EXPECTED_APPS[ref.engine]
            or _text(job.get("dir")) != str(job_dir)
            or not _text(job.get("generation"))
            or not isinstance(recovery, dict)
            or recovery.get("pending") is not False
            or not isinstance(process, dict)
            or process.get("worker_pid") is not None
        ):
            _blocked(f"{ref.engine} job state identity mismatch: {ref.relative_dir}")

        matching = [entry for entry in entries if entry.queue_id == ref.queue_id]
        entry = matching[0] if len(matching) == 1 else None
        if len(matching) > 1:
            _blocked(f"queue identity is duplicated: {ref.queue_id}")
        for other in entries:
            if other.queue_id != ref.queue_id and _text(other.metadata.get("job_dir")) == str(
                job_dir
            ):
                _blocked(f"{ref.engine} job directory is shared by another queue generation")

        if entry is None:
            status_payload = state.get("status")
            if not isinstance(status_payload, dict) or (
                _text(status_payload.get("state")).lower() != "completed"
                or _text(status_payload.get("reason")) != "completed"
                or status_payload.get("exit_code") != 0
            ):
                _blocked(f"{ref.engine} index fallback lacks canonical completed state")
            _verify_index_fallback(index, ref=ref, job_id=job_id, job_dir=job_dir)
        elif (
            entry.status != QueueStatus.COMPLETED
            or entry.cancel_requested
            or entry.app_name != _EXPECTED_APPS[ref.engine]
            or entry.engine != ref.engine
            or entry.task_id != job_id
            or _text(entry.metadata.get("job_dir")) != str(job_dir)
            or _text(entry.metadata.get("terminal_repair_blocked_reason"))
            or _text(entry.metadata.get("_orca_auto_queued_record_sync")).lower()
            not in {"", "complete"}
            or _text(job.get("generation")) != queue_entry_generation_token(entry)
            or not terminal_state_is_consistent(
                state=state,
                entry=entry,
                engine=ref.engine,
                job_dir=job_dir,
                expected_status="completed",
                expected_reason="completed",
            )
        ):
            _blocked(f"{ref.engine} queue/state terminal identity mismatch")
        verified.append(_VerifiedJob(ref=ref, job_id=job_id))
    return tuple(verified)


def _verify_no_snapshot_intents(queue_roots: Sequence[Path]) -> None:
    for root in queue_roots:
        intent_dir = root / _SNAPSHOT_INTENT_DIR_NAME
        if not intent_dir.exists():
            continue
        _owned_directory(intent_dir, label=f"snapshot intent directory {root}")
        try:
            next(intent_dir.iterdir())
        except StopIteration:
            continue
        _blocked(f"queue root still has snapshot intent state: {root}")


def _verify_no_admission_slots(
    admission_root: Path,
    *,
    workflow_id: str,
    workspace: Path,
    refs: Sequence[_JobRef],
) -> None:
    if admission_root.exists():
        _owned_directory(admission_root, label="admission root")
    try:
        slots = list_all_slots(admission_root)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        _blocked(f"admission state is unreadable: {exc}")
    queue_ids = {ref.queue_id for ref in refs}
    job_dirs = {str(workspace.joinpath(*ref.relative_dir.parts)) for ref in refs}
    app_tasks = {(_EXPECTED_APPS[ref.engine], ref.expected_job_id) for ref in refs}
    for slot in slots:
        work_dir = _text(slot.work_dir)
        try:
            work_path = Path(work_dir).expanduser().resolve() if work_dir else None
        except OSError:
            work_path = None
        if (
            _text(slot.workflow_id) == workflow_id
            or _text(slot.queue_id) in queue_ids
            or _text(slot.work_dir) in job_dirs
            or (
                work_path is not None and (work_path == workspace or workspace in work_path.parents)
            )
            or (_text(slot.app_name), _text(slot.task_id)) in app_tasks
        ):
            _blocked("workflow still has related admission state")


def _candidate_for_leaf(
    workspace: Path,
    job: _VerifiedJob,
    leaf: str,
) -> _Candidate | None:
    job_dir = workspace.joinpath(*job.ref.relative_dir.parts)
    path = job_dir / leaf
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _blocked(f"report candidate cannot be inspected: {path}: {exc}")
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != os.geteuid()
    ):
        _blocked(f"report candidate is not an owned single-link regular file: {path}")
    return _Candidate(
        artifact=CompactionArtifact(
            relative_path=str(job.ref.relative_dir / leaf),
            size=int(details.st_size),
            engine=job.ref.engine,
            stage_id=job.ref.stage_id,
            job_id=job.job_id,
        ),
        device=int(details.st_dev),
        inode=int(details.st_ino),
    )


def _plan_candidates(
    workspace: Path,
    jobs: Sequence[_VerifiedJob],
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    total = 0
    for job in jobs:
        for leaf in _REPORT_LEAVES:
            candidate = _candidate_for_leaf(workspace, job, leaf)
            if candidate is None:
                continue
            candidates.append(candidate)
            total += candidate.artifact.size
            if len(candidates) > _MAX_CANDIDATES:
                _blocked("compaction candidate count exceeds the configured bound")
            if total > _MAX_REPORT_BYTES:
                _blocked("compaction candidate bytes exceed the configured bound")
    return tuple(sorted(candidates, key=lambda item: item.artifact.relative_path))


def _unlink_candidate(workspace: Path, candidate: _Candidate) -> None:
    relative = PurePosixPath(candidate.artifact.relative_path)
    if relative.name not in _REPORT_LEAVES or len(relative.parts) < 3:
        _blocked("compaction candidate is outside the fixed report allowlist")
    parent = workspace.joinpath(*relative.parts[:-1])
    _owned_directory(parent, label="report candidate directory")
    dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(parent, dir_flags)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(relative.name, file_flags, dir_fd=directory_fd)
        except OSError as exc:
            _blocked(f"report candidate changed before deletion: {relative}: {exc}")
        try:
            opened = os.fstat(descriptor)
            named = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (candidate.device, candidate.inode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size != candidate.artifact.size
            ):
                _blocked(f"report candidate identity changed before deletion: {relative}")
            os.unlink(relative.name, dir_fd=directory_fd)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _result(
    *,
    workflow_id: str,
    workspace: Path,
    eligible: bool,
    applied: bool,
    candidates: Sequence[_Candidate] = (),
    removed: Sequence[str] = (),
    removed_bytes: int = 0,
    reasons: Sequence[str] = (),
) -> WorkflowCompactionResult:
    return WorkflowCompactionResult(
        workflow_id=workflow_id,
        workspace_dir=str(workspace),
        eligible=eligible,
        blocked=not eligible,
        applied=applied,
        would_remove=tuple(candidate.artifact for candidate in candidates),
        removed=tuple(removed),
        removed_bytes=removed_bytes,
        preserved=_PRESERVED,
        reasons=tuple(reasons),
    )


def compact_completed_workflow(
    workflow_root: str | Path,
    workspace_dir: str | Path,
    *,
    apply: bool = False,
    admission_root: str | Path | None = None,
) -> WorkflowCompactionResult:
    """Plan or remove obsolete internal xTB/CREST report copies.

    The operation is idempotent. A partial process interruption needs no
    recovery journal: a later dry run simply plans the remaining report files.
    """

    root = _owned_directory(Path(workflow_root), label="workflow root")
    workspace = _owned_directory(Path(workspace_dir), label="workflow workspace")
    if not is_workflow_workspace_location(workspace, root):
        raise ValueError("workflow workspace is not a canonical child of workflow_root")
    resolved_admission_root = (
        Path(admission_root if admission_root is not None else default_shared_admission_root(root))
        .expanduser()
        .absolute()
    )

    workflow_id = workspace.name
    candidates: tuple[_Candidate, ...] = ()
    removed: list[str] = []
    removed_bytes = 0
    with acquire_workflow_lock(workspace):
        try:
            payload = _json_object(workspace / WORKFLOW_FILE_NAME, label="workflow payload")
            workflow_id, refs = _derive_job_refs(payload, workspace=workspace)
            queue_roots = tuple(sorted({ref.queue_root for ref in refs}, key=str))
            with ExitStack() as locks:
                locks.enter_context(admission_lock(resolved_admission_root))
                for queue_root in queue_roots:
                    locks.enter_context(queue_lock(queue_root))
                for queue_root in queue_roots:
                    locks.enter_context(tmpfs_file_lock(queue_root / _SNAPSHOT_MUTATION_LOCK_NAME))
                for queue_root in queue_roots:
                    locks.enter_context(tmpfs_file_lock(queue_root / JOB_LOCATION_INDEX_LOCK_NAME))
                locks.enter_context(tmpfs_file_lock(root / WORKFLOW_REGISTRY_LOCK_NAME))

                _verify_no_admission_slots(
                    resolved_admission_root,
                    workflow_id=workflow_id,
                    workspace=workspace,
                    refs=refs,
                )
                _verify_no_snapshot_intents(queue_roots)
                _registry_is_exact(root, workflow_id=workflow_id, workspace=workspace)
                jobs = _verify_jobs(workspace, refs)
                candidates = _plan_candidates(workspace, jobs)
                if not apply:
                    return _result(
                        workflow_id=workflow_id,
                        workspace=workspace,
                        eligible=True,
                        applied=False,
                        candidates=candidates,
                        reasons=("eligible dry-run; no filesystem mutation performed",),
                    )

                for candidate in candidates:
                    _unlink_candidate(workspace, candidate)
                    removed.append(candidate.artifact.relative_path)
                    removed_bytes += candidate.artifact.size
                return _result(
                    workflow_id=workflow_id,
                    workspace=workspace,
                    eligible=True,
                    applied=True,
                    candidates=candidates,
                    removed=removed,
                    removed_bytes=removed_bytes,
                    reasons=("compaction applied to exact internal report copies",),
                )
        except _Blocked as exc:
            return _result(
                workflow_id=workflow_id,
                workspace=workspace,
                eligible=False,
                applied=False,
                candidates=candidates,
                removed=removed,
                removed_bytes=removed_bytes,
                reasons=(str(exc),),
            )


__all__ = [
    "CompactionArtifact",
    "WorkflowCompactionResult",
    "compact_completed_workflow",
]
