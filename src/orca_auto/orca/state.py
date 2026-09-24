"""Normalize and persist ORCA run state with generation ownership checks."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core import activity_invalidation as _activity_invalidation
from orca_auto.core.confined_io import (
    atomic_write_confined_bytes,
)
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils.lock import file_lock_at
from orca_auto.core.utils.persistence import (
    atomic_write_text as _atomic_write_text,
)
from orca_auto.core.utils.persistence import (
    durable_mkdir,
    timestamped_token,
)
from orca_auto.core.utils.persistence import (
    now_utc_iso as _now_utc_iso,
)
from orca_auto.orca.engine_artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)

from . import state_reading as _state_reading
from .statuses import (
    ACTIVE_RUN_STATUS_VALUES,
    TERMINAL_RUN_STATUSES,
    AnalyzerStatus,
    RunStatus,
    coerce_run_status,
)
from .types import RunFinalResult, RunState

logger = logging.getLogger(__name__)


STATE_MUTATION_LOCK_FILE_NAME = ".job_state.mutation.lock"


def now_utc_iso() -> str:
    return _now_utc_iso()


def _write_generation_json(
    target: tuple[Path, tuple[int, int]],
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    write_generation_bytes(
        target,
        path,
        json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=False,
            allow_nan=False,
        ).encode("utf-8"),
    )


def write_generation_bytes(
    target: tuple[Path, tuple[int, int]],
    path: Path,
    payload: bytes,
) -> None:
    generation_dir, generation_identity = target
    atomic_write_confined_bytes(
        generation_dir,
        path,
        payload,
        label="ORCA generation artifact",
        mode=0o600,
        expected_parent_identity=generation_identity,
    )


def new_state(reaction_dir: Path, selected_inp: Path) -> RunState:
    run_id = timestamped_token("run", token_bytes=16)
    ts = now_utc_iso()
    return {
        "run_id": run_id,
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "status": RunStatus.CREATED.value,
        "started_at": ts,
        "updated_at": ts,
        "attempts": [],
        "scratch_publications": [],
        "final_result": None,
    }


atomic_write_text = _atomic_write_text


def write_state(reaction_dir: Path, state: Mapping[str, Any]) -> Path:
    state_payload = dict(state)
    state_payload["updated_at"] = now_utc_iso()
    path = _state_reading.state_path(reaction_dir)
    durable_mkdir(reaction_dir, parents=True, exist_ok=True)
    payload = normalized_payload_from_state(reaction_dir, state_payload)
    directory_fd = os.open(
        reaction_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        directory_status = os.fstat(directory_fd)
        directory_identity = (
            int(directory_status.st_dev),
            int(directory_status.st_ino),
        )
        with file_lock_at(
            directory_fd,
            STATE_MUTATION_LOCK_FILE_NAME,
            display_path=reaction_dir / STATE_MUTATION_LOCK_FILE_NAME,
        ):
            _activity_invalidation.invalidate_state(reaction_dir)
            generation_target = _state_reading.verified_generation_artifact_target(
                reaction_dir, state_payload
            )
            if generation_target is not None and not retired_generation(generation_target[0]):
                _write_generation_json(
                    generation_target,
                    _state_reading.state_path(generation_target[0]),
                    payload,
                )
            atomic_write_confined_bytes(
                reaction_dir,
                path,
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=False,
                    allow_nan=False,
                ).encode("utf-8"),
                label="ORCA state",
                mode=0o600,
                expected_parent_identity=directory_identity,
            )
    finally:
        os.close(directory_fd)
    if isinstance(state, dict):
        state["updated_at"] = state_payload["updated_at"]
    logger.debug("State saved: %s", path)
    return path


def save_state(reaction_dir: Path, state: Mapping[str, Any]) -> Path:
    return write_state(reaction_dir, state)


def finalize_state(
    reaction_dir: Path,
    state: RunState,
    *,
    status: RunStatus | str,
    final_result: RunFinalResult,
) -> None:
    """Persist the terminal ``status`` and ``final_result`` of a generation.

    ``status`` must be a member of ``TERMINAL_RUN_STATUSES`` (or its value);
    any other status raises ``ValueError`` before the state file is touched.
    """

    terminal_status = coerce_run_status(status)
    if terminal_status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"finalize_state requires a terminal run status, got {status!r}")
    state["status"] = terminal_status.value
    state["final_result"] = final_result
    write_state(reaction_dir, state)


def normalized_payload_from_state(reaction_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    scratch_publications = state.get("scratch_publications")
    if not isinstance(scratch_publications, list):
        scratch_publications = []
    final_result = state.get("final_result")
    final_result_payload = _dict(final_result)
    selected_inp = _state_reading.normalized_text(state.get("selected_inp"))
    status = _state_reading.normalized_text(state.get("status"))
    reason = _state_reading.normalized_text(final_result_payload.get("reason"))
    job_id = _state_reading.normalized_text(state.get("job_id")) or _state_reading.normalized_text(
        state.get("run_id")
    )
    last_out_path = _state_reading.normalized_text(final_result_payload.get("last_out_path"))
    updated_at = _state_reading.normalized_text(state.get("updated_at")) or now_utc_iso()
    return build_engine_artifact_payload(
        engine="orca",
        job=EngineArtifactJob(
            id=job_id,
            queue_id=_state_reading.normalized_text(state.get("queue_id")),
            dir=_state_reading.normalized_text(state.get("reaction_dir"))
            or str(reaction_dir.resolve()),
            app_name="orca_auto_orca",
            task_id=job_id,
            generation=_state_reading.normalized_text(state.get("queue_generation")),
        ),
        status=EngineArtifactStatus(
            state=status,
            reason=reason,
            exit_code=None,
        ),
        input=EngineArtifactInput(
            primary_path=selected_inp,
            selected_xyz_path="",
        ),
        resources=EngineArtifactResources(request={}, actual={}),
        timestamps=EngineArtifactTimestamps(
            created_at=_state_reading.normalized_text(state.get("started_at")),
            started_at=_state_reading.normalized_text(state.get("started_at")),
            updated_at=updated_at,
            finished_at=_state_reading.normalized_text(final_result_payload.get("completed_at")),
        ),
        recovery=EngineArtifactRecovery(
            pending=False,
            reason="",
            count=0,
            resumed=bool(final_result_payload.get("resumed", False)),
        ),
        artifacts={
            "manifest_path": "",
            "stdout_log": "",
            "stderr_log": "",
            "last_out_path": last_out_path,
        },
        engine_payload={
            "run_id": _state_reading.normalized_text(state.get("run_id")),
            "attempts": attempts,
            "scratch_publications": scratch_publications,
            "execution_provenance": _dict(state.get("execution_provenance")),
            "final_result": final_result,
        },
    )


def retired_generation(generation_dir: Path) -> bool:
    """Keep pre-removal generation artifacts immutable; root bookkeeping stays current."""
    if not _state_reading.state_path(generation_dir).exists():
        return False
    loaded = _state_reading.load_generation_state(generation_dir)
    if loaded is None:
        raise ValueError("Cannot classify an unreadable ORCA generation state")
    payload, _state = loaded
    return "max_retries" in _dict(payload.get("engine_payload"))


# --- attempt decisions and resumability ------------------------------------

RESUMABLE_RUN_STATUSES = ACTIVE_RUN_STATUS_VALUES
RESUMABLE_FAILED_REASONS = frozenset({"interrupted_by_user", "worker_shutdown", "crashed_recovery"})


@dataclass(frozen=True)
class AttemptDecision:
    run_status: RunStatus
    reason: str
    exit_code: int


def parse_analyzer_status(status_text: AnalyzerStatus | str) -> AnalyzerStatus | None:
    if isinstance(status_text, AnalyzerStatus):
        return status_text
    try:
        return AnalyzerStatus(str(status_text))
    except ValueError:
        return None


def decide_attempt_outcome(
    *,
    analyzer_status: AnalyzerStatus | str,
    analyzer_reason: str,
) -> AttemptDecision:
    parsed = parse_analyzer_status(analyzer_status)
    if parsed == AnalyzerStatus.COMPLETED:
        return AttemptDecision(run_status=RunStatus.COMPLETED, reason=analyzer_reason, exit_code=0)
    return AttemptDecision(run_status=RunStatus.FAILED, reason=analyzer_reason, exit_code=1)


def state_matches_selected(
    state: RunState,
    selected_inp: Path,
    *,
    to_resolved_local: Callable[[str], Path],
) -> bool:
    selected = state.get("selected_inp")
    if not isinstance(selected, str) or not selected.strip():
        return False
    try:
        return to_resolved_local(selected) == selected_inp.resolve()
    except Exception:  # noqa: BLE001
        return False


def _final_reason(state: RunState) -> str:
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return ""
    reason = final_result.get("reason")
    if not isinstance(reason, str):
        return ""
    return reason.strip()


def is_resumable_state(state: RunState) -> bool:
    status = str(state.get("status", "")).strip()
    if status in RESUMABLE_RUN_STATUSES:
        return True
    if status == RunStatus.FAILED.value:
        return _final_reason(state) in RESUMABLE_FAILED_REASONS
    return False


def load_or_create_state(
    reaction_dir: Path,
    selected_inp: Path,
    *,
    to_resolved_local: Callable[[str], Path],
) -> tuple[RunState, bool]:
    state = _state_reading.load_state(reaction_dir)
    resumed = False
    if not state or not state_matches_selected(
        state, selected_inp, to_resolved_local=to_resolved_local
    ):
        state = new_state(reaction_dir, selected_inp)
    elif is_resumable_state(state):
        resumed = True
        if state.get("final_result") is not None:
            state["final_result"] = None
    else:
        state = new_state(reaction_dir, selected_inp)

    if not isinstance(state.get("attempts"), list):
        state["attempts"] = []
    save_state(reaction_dir, state)
    return state, resumed
