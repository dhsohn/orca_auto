"""Own execution-state persistence and the current job-root operational view."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from orca_auto.core import activity_invalidation as _activity_invalidation
from orca_auto.core.artifacts import STATE_MUTATION_LOCK_FILE_NAME
from orca_auto.core.confined_io import (
    atomic_write_confined_bytes,
)
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils.lock import file_lock_at
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
from .app_ids import ORCA_AUTO_ORCA_APP_NAME
from .generation_validation import is_retired_generation_marker
from .statuses import (
    TERMINAL_RUN_STATUSES,
    RunStatus,
    coerce_run_status,
)
from .types import RunFinalResult, RunState

logger = logging.getLogger(__name__)


def now_utc_iso() -> str:
    return _now_utc_iso()


def _existing_generation_payload(generation_dir: Path) -> dict[str, Any] | None:
    if not _state_reading.state_path(generation_dir).exists():
        return None
    loaded = _state_reading.load_generation_state(generation_dir)
    if loaded is None:
        raise ValueError("Cannot classify an unreadable ORCA generation state")
    return loaded[0]


def _generation_execution_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep execution facts; notification delivery belongs to the current root."""
    execution = deepcopy(dict(payload))
    final_result = _dict(execution.get("engine_payload")).get("final_result")
    if isinstance(final_result, dict):
        final_result.pop("finished_notification_claimed_at", None)
        final_result.pop("finished_notification_sent_at", None)
    return execution


def _write_generation_state(
    target: tuple[Path, tuple[int, int]],
    payload: Mapping[str, Any],
) -> None:
    existing = _existing_generation_payload(target[0])
    if existing is not None and is_retired_generation_marker(_dict(existing.get("engine_payload"))):
        return
    execution = _generation_execution_payload(payload)
    if existing is not None:
        recorded = _generation_execution_payload(existing)
        # Root bookkeeping advances its own timestamp. An identical execution
        # record keeps its original bytes, timestamp and historical fields.
        recorded["timestamps"] = {
            **_dict(recorded.get("timestamps")),
            "updated_at": _dict(execution.get("timestamps")).get("updated_at"),
        }
        if recorded == execution:
            return
    write_generation_bytes(
        target,
        _state_reading.state_path(target[0]),
        json.dumps(
            execution,
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


def save_state(reaction_dir: Path, state: Mapping[str, Any]) -> Path:
    """Save changed generation evidence before refreshing the current root view.

    Both writes share the root mutation lock. A failed root refresh leaves the
    generation evidence intact; retrying the same execution only refreshes root.
    """
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
            if generation_target is not None:
                _write_generation_state(generation_target, payload)
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
    save_state(reaction_dir, state)


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
            app_name=ORCA_AUTO_ORCA_APP_NAME,
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
    payload = _existing_generation_payload(generation_dir)
    return payload is not None and is_retired_generation_marker(
        _dict(payload.get("engine_payload"))
    )
