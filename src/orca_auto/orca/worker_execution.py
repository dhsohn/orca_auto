from __future__ import annotations

import copy
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.engine_scratch import (
    EngineScratchCapacityError,
    attach_scratch_provenance_mapping_to_exception,
    scratch_provenance_from_exception,
)
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.engines.worker_child import (
    WORKER_CHILD_MODULE,
    build_worker_child_command_for_engine,
)
from orca_auto.core.queue.child.execution import (
    ChildWorkerShutdownController,
    find_queue_entry_by_id,
)
from orca_auto.core.queue.child.process import entry_status_is_running
from orca_auto.core.queue.engine.child import await_parent_admission_handoff
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_TOKEN_KEY,
    transition_snapshot_intent,
)
from orca_auto.core.queue.generation import (
    is_visible_generation_name,
    new_visible_generation_name,
    queue_entry_generation_token,
)
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
)
from orca_auto.core.utils.persistence import timestamped_token, timestamped_token_pattern

from .admission_env import (
    ADMISSION_APP_NAME_ENV_VAR,
    ADMISSION_TASK_ID_ENV_VAR,
    ADMISSION_TOKEN_ENV_VAR,
)
from .attempt.reporting import build_final_result, last_out_path_from_state
from .config import AppConfig, load_config
from .execution import execute_orca_run, existing_completed_out, recover_crashed_state
from .execution_binding import (
    ORCA_EXECUTION_SNAPSHOT_VERSION,
    build_orca_execution_snapshot,
    cleanup_unowned_orca_execution_snapshot,
    orca_execution_provenance,
    orca_execution_snapshot_generation_dir,
    orca_execution_started_evidence,
    verify_orca_execution_snapshot,
    verify_orca_snapshot_executable,
)
from .orca_runner import OrcaRunner, RunResult, WorkerShutdownInterrupt
from .queue.adapter import (
    cancellation_probe,
    get_cancel_requested,
    list_queue,
    mark_failed,
    queue_entry_app_name,
    queue_entry_force,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    requeue_running_entry,
    update_metadata,
)
from .queue.entries import queue_entry_is_retired_workflow_owned
from .resource_directives import prepare_submission_resource_request
from .run_context import RunExecutionContext, configured_admission_root
from .run_lock import acquire_run_lock
from .state import finalize_state
from .state_reading import load_state
from .statuses import AnalyzerStatus
from .submission import mark_orca_snapshot_owned

logger = logging.getLogger(__name__)

# EX_TEMPFAIL: the child returned its row to the queue before ORCA started.
ADMISSION_DEFERRED_EXIT_CODE = 75
RECOVERY_REBIND_LIMIT = 3
RECOVERY_REBIND_COUNT_METADATA_KEY = "recovery_rebind_count"
RECOVERY_REBIND_CLAIM_METADATA_KEY = "recovery_rebind_claim"
# The durable claim replays only tokens this module minted; the validator is
# derived from the producer so the two cannot drift apart.
_RECOVERY_INTENT_TOKEN_PREFIX = "snapshot_intent"
_RECOVERY_INTENT_TOKEN_BYTES = 16
_RECOVERY_REBIND_INTENT_TOKEN_RE = timestamped_token_pattern(
    _RECOVERY_INTENT_TOKEN_PREFIX,
    token_bytes=_RECOVERY_INTENT_TOKEN_BYTES,
)

BackgroundRunJobProcess = subprocess.Popen
WORKER_JOB_MODULE = WORKER_CHILD_MODULE


@dataclass(frozen=True)
class OrcaWorkerExecutionContext:
    entry: QueueEntry
    reaction_dir: str
    force: bool
    admission_token: str | None
    admission_app_name: str | None
    admission_task_id: str | None
    selected_inp: str
    source_selected_inp: str
    selected_input_xyz: str
    resource_request: dict[str, int]
    execution_snapshot: dict[str, Any]
    orca_executable: str

    def verify_snapshot(self, *, allow_runtime_outputs: bool) -> tuple[Path, str]:
        return verify_orca_execution_snapshot(
            self.reaction_dir,
            self.execution_snapshot,
            expected_selected_inp=self.selected_inp,
            expected_source_selected_inp=self.source_selected_inp,
            expected_selected_input_xyz=self.selected_input_xyz,
            expected_resource_request=self.resource_request,
            allow_runtime_outputs=allow_runtime_outputs,
        )


@dataclass(frozen=True)
class OrcaWorkerExecutionOutcome:
    exit_code: int
    reaction_dir: str
    entry: QueueEntry


build_worker_child_command = build_worker_child_command_for_engine("orca")


class WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: OrcaWorkerExecutionContext) -> None:
        super().__init__("worker_shutdown")
        self.context = context


def _explicit_or_env(value: str | None, env_var: str) -> str | None:
    if value is not None:
        return value
    return (os.getenv(env_var, "") or "").strip() or None


def _completed_out_or_none(bound_selected: Path) -> dict[str, Any] | None:
    try:
        return existing_completed_out(bound_selected)
    except Exception:  # noqa: BLE001
        # The output in a crashed generation is exactly the file most likely
        # to be truncated or actively racing; a probe failure must degrade to
        # the recovery path, never abort the claim.
        logger.debug(
            "completed-output probe failed for %s; continuing with recovery",
            bound_selected,
            exc_info=True,
        )
        return None


def _queue_entry_by_id(queue_root: Path, queue_id: str) -> QueueEntry | None:
    return find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue,
    )


def _build_execution_context(
    cfg: AppConfig,
    entry: QueueEntry,
    *,
    admission_token: str | None,
) -> OrcaWorkerExecutionContext:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    if "max_retries" in metadata:
        raise ValueError("Queued ORCA entry contains a removed execution setting; resubmit the job")
    raw_reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser()
    reaction_dir = raw_reaction_dir.resolve()
    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    if (
        raw_reaction_dir.is_symlink()
        or not reaction_dir.is_relative_to(allowed_root)
        or not reaction_dir.is_dir()
    ):
        raise ValueError("Queued ORCA reaction directory is outside the configured root")
    if queue_entry_is_retired_workflow_owned(entry, allowed_root):
        raise ValueError(
            "Queued ORCA directory belongs to a retired workflow; use the previous runtime to drain or cancel it"
        )
    selected_inp = str(metadata.get("selected_inp") or "").strip()
    source_selected_inp = str(metadata.get("source_selected_inp") or "").strip()
    selected_input_xyz = str(metadata.get("selected_input_xyz") or "").strip()
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError(
            "Queued ORCA entry predates immutable execution snapshots; drain or resubmit it"
        )
    resource_request = metadata.get("resource_request")
    if (
        not isinstance(resource_request, dict)
        or set(resource_request) != {"max_cores", "max_memory_gb"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in resource_request.values()
        )
    ):
        raise ValueError("Queued ORCA entry has no resource request")
    # A generation whose bound input already has a completed, analyzer-verified
    # output legitimately carries runtime files: allow them so the run can
    # settle the finished generation in place instead of dying on the pristine
    # check (the crash landed after ORCA finished but before the queue row
    # turned terminal). The run settles from a recorded attempt verdict when
    # one exists, which may be a failure, and adopts the output otherwise.
    selected_inp_path = Path(selected_inp) if selected_inp else None
    completed_adoption = bool(
        selected_inp_path is not None
        and selected_inp_path.is_file()
        and _completed_out_or_none(selected_inp_path) is not None
    )
    verified_selected, orca_executable = verify_orca_execution_snapshot(
        reaction_dir,
        snapshot,
        expected_selected_inp=selected_inp,
        expected_source_selected_inp=source_selected_inp,
        expected_selected_input_xyz=selected_input_xyz,
        expected_resource_request=resource_request,
        allow_runtime_outputs=completed_adoption,
    )
    return OrcaWorkerExecutionContext(
        entry=entry,
        reaction_dir=str(reaction_dir),
        force=queue_entry_force(entry),
        admission_token=admission_token,
        admission_app_name=queue_entry_app_name(entry) or None,
        admission_task_id=queue_entry_task_id(entry) or None,
        selected_inp=str(verified_selected),
        source_selected_inp=source_selected_inp,
        selected_input_xyz=selected_input_xyz,
        resource_request=dict(resource_request),
        execution_snapshot=snapshot,
        orca_executable=orca_executable,
    )


def _run_orca_job_for_entry(
    cfg: AppConfig,
    context: OrcaWorkerExecutionContext,
    queue_root: Path,
    *,
    should_cancel: Callable[[], bool],
    shutdown_requested: Callable[[], bool] | None,
) -> int:
    if shutdown_requested is not None and shutdown_requested():
        raise WorkerShutdownRequested(context)
    execution_provenance = orca_execution_provenance(context.execution_snapshot)

    def stop_requested() -> bool:
        return should_cancel() or (shutdown_requested is not None and shutdown_requested())

    class ShutdownAwareOrcaRunner(OrcaRunner):
        def __init__(self, _configured_orca_executable: str) -> None:
            super().__init__(context.orca_executable)
            self._runtime_outputs_started = False
            self.set_executable_identity(
                context.execution_snapshot["executable_identities"]["orca"]
            )
            self.set_durable_directory_identity(
                context.execution_snapshot["execution_dir_identity"]
            )
            self.set_shutdown_requested(stop_requested)

        def prepare(self, inp_path: Path) -> None:
            # Staging reads the generation, so keep run()'s verify-before-stage
            # order. A snapshot that fails here is left for run() to report.
            try:
                context.verify_snapshot(allow_runtime_outputs=False)
            except Exception:  # noqa: BLE001
                return
            super().prepare(inp_path)

        def run(self, inp_path: Path) -> RunResult:
            current_input = require_confined_regular_file(
                Path(context.execution_snapshot["execution_dir"]),
                inp_path,
                label="ORCA queued execution input",
            )
            if (
                current_input.parent != Path(context.execution_snapshot["execution_dir"]).resolve()
                or current_input.suffix.lower() != ".inp"
            ):
                raise ValueError("ORCA queued execution input must be a private .inp file")
            context.verify_snapshot(allow_runtime_outputs=self._runtime_outputs_started)
            try:
                result = super().run(inp_path)
            except BaseException as run_exc:
                self._runtime_outputs_started = True
                try:
                    context.verify_snapshot(allow_runtime_outputs=True)
                except BaseException as verify_exc:
                    provenance = scratch_provenance_from_exception(run_exc)
                    if provenance:
                        attach_scratch_provenance_mapping_to_exception(verify_exc, provenance)
                    raise
                raise
            self._runtime_outputs_started = True
            try:
                context.verify_snapshot(allow_runtime_outputs=True)
            except BaseException as verify_exc:
                result_provenance = getattr(result, "scratch_provenance", None)
                if isinstance(result_provenance, dict) and result_provenance:
                    attach_scratch_provenance_mapping_to_exception(
                        verify_exc,
                        result_provenance,
                    )
                raise
            result.execution_provenance = dict(execution_provenance)
            return result

    bound_cfg = copy.copy(cfg)
    bound_cfg.runtime = copy.copy(cfg.runtime)
    bound_cfg.resources = replace(
        cfg.resources,
        max_cores_per_task=context.resource_request["max_cores"],
        max_memory_gb_per_task=context.resource_request["max_memory_gb"],
    )

    try:
        execution = RunExecutionContext(
            cfg=bound_cfg,
            reaction_dir=Path(context.reaction_dir).expanduser().resolve(),
            selected_inp=Path(context.selected_inp).expanduser().resolve(),
            admission_root=configured_admission_root(bound_cfg),
            force=context.force,
            reservation_token=_explicit_or_env(context.admission_token, ADMISSION_TOKEN_ENV_VAR),
            admission_app_name=_explicit_or_env(
                context.admission_app_name, ADMISSION_APP_NAME_ENV_VAR
            ),
            admission_task_id=_explicit_or_env(
                context.admission_task_id, ADMISSION_TASK_ID_ENV_VAR
            ),
            execution_provenance=dict(execution_provenance),
            queue_id=queue_entry_id(context.entry) or None,
            queue_generation=queue_entry_generation_token(context.entry) or None,
        )
        return execute_orca_run(execution, runner_cls=ShutdownAwareOrcaRunner)
    except EngineScratchCapacityError as exc:
        return _defer_admission(context, queue_root, reason=str(exc))
    except WorkerShutdownInterrupt as exc:
        if should_cancel():
            state = load_state(Path(context.reaction_dir))
            if state is not None and not isinstance(state.get("final_result"), dict):
                cancelled_result = build_final_result(
                    status="cancelled",
                    analyzer_status=AnalyzerStatus.INCOMPLETE,
                    reason="cancel_requested",
                    last_out_path=last_out_path_from_state(state),
                )
                finalize_state(
                    Path(context.reaction_dir),
                    state,
                    status="cancelled",
                    final_result=cancelled_result,
                )
        raise WorkerShutdownRequested(context) from exc


def _defer_admission(
    context: OrcaWorkerExecutionContext,
    queue_root: Path,
    *,
    reason: str,
) -> int:
    """Return a job that never started to the queue instead of failing it.

    The refusal was raised before the run wrote any state, so the generation is
    still pristine and the next claim reuses it without a recovery rebind. This
    re-asks for a resource; it never reruns a calculation.
    """
    queue_id = queue_entry_id(context.entry)
    requeued = requeue_running_entry(
        queue_root,
        queue_id,
        expected_entry=context.entry,
        admission_deferral_reason=reason,
    )
    if requeued:
        logger.warning("ORCA job %s waits for RAM scratch capacity: %s", queue_id, reason)
    else:
        logger.error(
            "ORCA job %s could not be returned to the queue after a RAM scratch "
            "capacity refusal: %s",
            queue_id,
            reason,
        )
    # Never zero: if the requeue was fenced out the row is still running, and
    # the parent must mark it failed rather than completed.
    return ADMISSION_DEFERRED_EXIT_CODE


def _validated_recovery_rebind_claim(
    metadata: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    """Validate the durable recovery budget and claim without performing I/O."""
    if (
        snapshot.get("version") != ORCA_EXECUTION_SNAPSHOT_VERSION
        or "max_retries" in snapshot
        or "max_retries" in metadata
    ):
        raise ValueError("ORCA recovery requires a current execution snapshot; resubmit the job")
    raw_count = metadata.get(RECOVERY_REBIND_COUNT_METADATA_KEY, 0)
    if (
        isinstance(raw_count, bool)
        or not isinstance(raw_count, int)
        or raw_count < 0
        or raw_count > RECOVERY_REBIND_LIMIT
    ):
        raise ValueError("ORCA crash recovery found an invalid durable rebind count")
    count = raw_count
    raw_claim = metadata.get(RECOVERY_REBIND_CLAIM_METADATA_KEY)
    pending_claim: dict[str, Any] | None = None
    if raw_claim is not None:
        if not isinstance(raw_claim, dict) or set(raw_claim) != {
            "ordinal",
            "source_generation_name",
            "intent_token",
            "target_generation_name",
        }:
            raise ValueError("ORCA crash recovery found an invalid durable rebind claim")
        ordinal = raw_claim.get("ordinal")
        raw_source_generation_name = raw_claim.get("source_generation_name")
        raw_intent_token = raw_claim.get("intent_token")
        raw_target_generation_name = raw_claim.get("target_generation_name")
        source_generation_name = (
            raw_source_generation_name if type(raw_source_generation_name) is str else ""
        )
        intent_token = raw_intent_token if type(raw_intent_token) is str else ""
        target_generation_name = (
            raw_target_generation_name if type(raw_target_generation_name) is str else ""
        )
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal <= 0
            or ordinal > RECOVERY_REBIND_LIMIT
            or ordinal != count
            or type(raw_source_generation_name) is not str
            or source_generation_name != str(snapshot.get("generation_name") or "")
            or type(raw_intent_token) is not str
            or _RECOVERY_REBIND_INTENT_TOKEN_RE.fullmatch(intent_token) is None
            or type(raw_target_generation_name) is not str
            or not is_visible_generation_name(target_generation_name)
            or target_generation_name == source_generation_name
        ):
            raise ValueError(
                "ORCA crash recovery durable rebind claim does not match the queue row"
            )
        pending_claim = dict(raw_claim)
    if raw_claim is None and count >= RECOVERY_REBIND_LIMIT:
        raise ValueError(
            "ORCA crash recovery limit reached for this submission "
            f"({count}/{RECOVERY_REBIND_LIMIT}); resubmit the job to continue"
        )
    return count, pending_claim


def _reserve_recovery_rebind_claim(
    entry: QueueEntry,
    *,
    queue_root: Path,
    snapshot: dict[str, Any],
    count: int,
    pending_claim: dict[str, Any] | None,
) -> tuple[QueueEntry, int, str, str]:
    """Reserve budget durably, or resume the already reserved generation."""
    if pending_claim is None:
        rebind_count = count + 1
        intent_token = timestamped_token(
            _RECOVERY_INTENT_TOKEN_PREFIX,
            token_bytes=_RECOVERY_INTENT_TOKEN_BYTES,
        )
        target_generation_name = new_visible_generation_name()
        pending_claim = {
            "ordinal": rebind_count,
            "source_generation_name": str(snapshot.get("generation_name") or ""),
            "intent_token": intent_token,
            "target_generation_name": target_generation_name,
        }
        if not update_metadata(
            queue_root,
            str(entry.queue_id),
            {
                RECOVERY_REBIND_COUNT_METADATA_KEY: rebind_count,
                RECOVERY_REBIND_CLAIM_METADATA_KEY: pending_claim,
            },
            expected_entry=entry,
        ):
            raise ValueError(
                "ORCA crash recovery could not reserve its durable rebind claim on the queue row"
            )
        claimed = _queue_entry_by_id(queue_root, str(entry.queue_id))
        claimed_metadata = getattr(claimed, "metadata", None)
        if (
            claimed is None
            or not isinstance(claimed_metadata, dict)
            or claimed_metadata.get(RECOVERY_REBIND_COUNT_METADATA_KEY) != rebind_count
            or claimed_metadata.get(RECOVERY_REBIND_CLAIM_METADATA_KEY) != pending_claim
        ):
            raise ValueError("ORCA crash recovery lost its durable rebind claim")
    else:
        rebind_count = count
        intent_token = str(pending_claim["intent_token"])
        target_generation_name = str(pending_claim["target_generation_name"])
        claimed = entry

    return claimed, rebind_count, intent_token, target_generation_name


def _publish_recovery_generation(
    entry: QueueEntry,
    *,
    queue_root: Path,
    reaction_dir: Path,
    claimed: QueueEntry,
    new_snapshot: dict[str, Any],
    rebind_count: int,
) -> QueueEntry:
    """Fence publication against cancellation and clean up an unowned replacement."""
    intent_token = str(new_snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "")
    try:
        transition_snapshot_intent(
            queue_root,
            intent_token,
            target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
            expected_states={SNAPSHOT_INTENT_STATE_CREATING},
        )
        if not update_metadata(
            queue_root,
            str(entry.queue_id),
            {
                "execution_snapshot": new_snapshot,
                "selected_inp": str(new_snapshot.get("selected_inp") or ""),
                RECOVERY_REBIND_CLAIM_METADATA_KEY: None,
            },
            expected_entry=claimed,
            require_running_without_cancel_requested=True,
        ):
            raise ValueError("ORCA crash recovery could not publish its replacement generation")
    except BaseException:
        cleanup_unowned_orca_execution_snapshot(reaction_dir, new_snapshot)
        raise
    marker_warning = mark_orca_snapshot_owned(queue_root, intent_token)
    updated = _queue_entry_by_id(queue_root, str(entry.queue_id))
    updated_metadata = getattr(updated, "metadata", None)
    updated_snapshot = (
        updated_metadata.get("execution_snapshot") if isinstance(updated_metadata, dict) else None
    )
    if (
        updated is None
        or not isinstance(updated_snapshot, dict)
        or str(updated_snapshot.get("generation_name") or "")
        != str(new_snapshot.get("generation_name") or "")
    ):
        raise ValueError("ORCA crash recovery lost its replacement queue row")
    logger.warning(
        "Recovered crashed ORCA job %s into replacement generation %s (rebind %d/%d)%s",
        str(entry.queue_id),
        str(new_snapshot.get("generation_name") or ""),
        rebind_count,
        RECOVERY_REBIND_LIMIT,
        f"; {marker_warning}" if marker_warning else "",
    )
    return updated


def _maybe_rebind_recovery_generation(
    entry: QueueEntry,
    *,
    queue_root: Path,
    cfg_factory: Callable[[], AppConfig],
) -> QueueEntry:
    """Move a crash-interrupted claim into a fresh generation before execution.

    A generation that shows started-execution evidence is never reused: the
    crashed generation stays frozen as that attempt's record and a replacement
    generation is materialized through the ordinary submission machinery,
    seeded from the frozen runtime geometry. Rebinds are bounded by a durable
    per-row counter that is consumed before any new generation exists, so a
    crash loop can never mint generations indefinitely.

    Runs where the worker child fixes its canonical queue entry, so every
    later actor (cancel checks, shutdown requeue, terminal marking) holds the
    post-rebind publication generation.
    """

    if not entry_status_is_running(entry):
        return entry
    if queue_entry_is_retired_workflow_owned(entry, queue_root):
        raise ValueError(
            "Queued ORCA directory belongs to a retired workflow; use the previous runtime to drain or cancel it"
        )
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, dict):
        return entry
    reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser().resolve()
    try:
        orca_execution_snapshot_generation_dir(reaction_dir, snapshot)
    except ValueError:
        # Let the ordinary context builder surface its canonical error.
        return entry
    if not orca_execution_started_evidence(reaction_dir, snapshot):
        return entry
    bound_selected_text = str(metadata.get("selected_inp") or "").strip()
    if bound_selected_text:
        bound_selected = Path(bound_selected_text)
        if bound_selected.is_file() and _completed_out_or_none(bound_selected) is not None:
            # ORCA finished before the crash reached the queue row. Keep the
            # generation: the ordinary claim path settles it in place (from
            # its recorded attempt verdict, else by adopting the output)
            # instead of re-running the whole calculation in a rebind.
            return entry
    if get_cancel_requested(queue_root, str(entry.queue_id), expected_entry=entry):
        # Cancellation is a generation-fenced monotonic user decision. Honor it
        # before inspecting recovery-only metadata or creating replacement state.
        requeue_running_entry(
            queue_root,
            str(entry.queue_id),
            expected_entry=entry,
        )
        refreshed = _queue_entry_by_id(queue_root, str(entry.queue_id))
        return refreshed if refreshed is not None else entry
    count, pending_claim = _validated_recovery_rebind_claim(metadata, snapshot)
    cfg = cfg_factory()
    recovery_executable = verify_orca_snapshot_executable(
        snapshot,
        expected_executable=cfg.paths.orca_executable,
    )
    claimed, rebind_count, intent_token, target_generation_name = _reserve_recovery_rebind_claim(
        entry,
        queue_root=queue_root,
        snapshot=snapshot,
        count=count,
        pending_claim=pending_claim,
    )

    source_selected = str(metadata.get("source_selected_inp") or "").strip()
    if not source_selected:
        raise ValueError("ORCA crash recovery requires the submission source input path")
    recorded_request = metadata.get("resource_request")
    with acquire_run_lock(reaction_dir):
        recover_crashed_state(reaction_dir, logger=logger)
        prepared = prepare_submission_resource_request(
            Path(source_selected),
            default_max_cores=int(cfg.resources.max_cores_per_task),
            default_max_memory_gb=int(cfg.resources.max_memory_gb_per_task),
        )
        if not isinstance(recorded_request, dict) or dict(prepared.resource_request) != dict(
            recorded_request
        ):
            raise ValueError("ORCA crash recovery resource request diverged from the queued row")
        new_snapshot = build_orca_execution_snapshot(
            reaction_dir,
            Path(source_selected),
            selected_input_xyz=str(metadata.get("selected_input_xyz") or ""),
            resource_request=prepared.resource_request,
            orca_executable=recovery_executable,
            queue_root=queue_root,
            snapshot_intent_token=intent_token,
            target_generation_name=target_generation_name,
            normalized_selected_payload=prepared.normalized_payload,
            source_selected_sha256=prepared.source_sha256,
            recovery_from=snapshot,
        )
    return _publish_recovery_generation(
        entry,
        queue_root=queue_root,
        reaction_dir=reaction_dir,
        claimed=claimed,
        new_snapshot=new_snapshot,
        rebind_count=rebind_count,
    )


def _record_worker_rejection(
    queue_root: Path,
    entry: QueueEntry,
    *,
    reason: str,
    expected_entry: QueueEntry | None = None,
) -> None:
    """Leave a fail-closed prelaunch rejection on the queue row before the child exits.

    The rejection ends the child before any engine runs, so without this the
    parent would only record ``exit_code=1``; the reason itself would survive in
    the journal alone. The rebind may already have written its durable claim
    onto the row, so recovery uses the row as it stands now. A fresh claim
    supplies its original publication generation instead. In both cases the
    mark is additionally fenced under the queue lock to the dequeue this child
    was handed: ``started_at`` is cleared by a requeue and re-stamped by every
    re-dequeue, while the rebind never touches it. ``mark_failed`` also refuses
    a row whose cancellation has already been requested, so a racing
    cancellation still wins.
    """

    queue_id = str(entry.queue_id)
    current = _queue_entry_by_id(queue_root, queue_id)
    recorded = current is not None and mark_failed(
        queue_root,
        queue_id,
        error=reason,
        publish_terminal_side_effects=not queue_entry_is_retired_workflow_owned(entry, queue_root),
        expected_entry=current if expected_entry is None else expected_entry,
        require_running_started_at=str(entry.started_at),
    )
    if not recorded:
        logger.info(
            "Worker rejection for %s left to the parent finalizer: %s",
            queue_id,
            reason,
        )


def _cancellation_requested(probe: Callable[[], bool]) -> bool:
    try:
        return probe()
    except QueueLockTimeoutError:
        return False


def process_dequeued_entry(
    cfg: AppConfig,
    entry: QueueEntry,
    *,
    queue_root: Path,
    admission_token: str | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
) -> OrcaWorkerExecutionOutcome:
    probe = cancellation_probe(queue_root, entry)
    try:
        context = _build_execution_context(cfg, entry, admission_token=admission_token)
    except (ValueError, OSError) as exc:
        _record_worker_rejection(
            queue_root,
            entry,
            reason=f"execution rejected: {exc}",
            expected_entry=entry,
        )
        raise
    if shutdown_requested is not None and shutdown_requested():
        raise WorkerShutdownRequested(context)
    result = _run_orca_job_for_entry(
        cfg,
        context,
        queue_root,
        should_cancel=lambda: _cancellation_requested(probe),
        shutdown_requested=shutdown_requested,
    )
    return OrcaWorkerExecutionOutcome(
        exit_code=int(result),
        reaction_dir=context.reaction_dir,
        entry=entry,
    )


def run_worker_child_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    await_parent_admission_handoff_fn: Callable[
        [str | Path, str], bool
    ] = await_parent_admission_handoff,
) -> int:
    controller = ChildWorkerShutdownController()
    cfg = load_config(config_path)
    resolved_queue_root = Path(queue_root).expanduser().resolve()
    entry = _queue_entry_by_id(resolved_queue_root, queue_id)
    if entry is not None and entry_matches_engine_identity(entry, "orca"):
        try:
            entry = _maybe_rebind_recovery_generation(
                entry,
                queue_root=resolved_queue_root,
                cfg_factory=lambda: load_config(config_path),
            )
        except (ValueError, FileExistsError) as exc:
            _record_worker_rejection(
                resolved_queue_root,
                entry,
                reason=f"crash recovery rejected: {exc}",
            )
            raise
    if entry is None or not (
        entry_status_is_running(entry) and entry_matches_engine_identity(entry, "orca")
    ):
        return 1
    if admission_token and not await_parent_admission_handoff_fn(
        resolve_admission_root(cfg),
        admission_token,
    ):
        return 1
    install_shutdown_signal_handlers(controller.request)
    # The parent owns final admission release, including shutdown and exceptions
    # between engine launch and publication of its durable process identity.
    try:
        outcome = process_dequeued_entry(
            cfg,
            entry,
            queue_root=resolved_queue_root,
            admission_token=admission_token,
            shutdown_requested=controller.is_requested,
        )
    except WorkerShutdownRequested:
        requeue_running_entry(
            resolved_queue_root,
            queue_id,
            expected_entry=entry,
            expected_task_id=queue_entry_task_id(entry) or None,
        )
        return 0
    return int(outcome.exit_code)


__all__ = [
    "BackgroundRunJobProcess",
    "OrcaWorkerExecutionContext",
    "OrcaWorkerExecutionOutcome",
    "WORKER_JOB_MODULE",
    "build_worker_child_command",
    "process_dequeued_entry",
    "run_worker_child_job",
]
