from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from orca_auto import __version__
from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import (
    MAX_RUN_ARTIFACT_JSON_BYTES,
    RUN_REPORT_HTML_FILE,
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
    SI_BLOCK_MD_FILE,
)
from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
    read_confined_text,
    require_confined_regular_file,
)
from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)
from orca_auto.core.machine_observation import (
    MACHINE_CONTRACT_NAME,
    MACHINE_CONTRACT_VERSION,
    RESULTS_PAYLOAD_CONTRACT_NAME,
    RESULTS_PAYLOAD_CONTRACT_VERSION,
    artifact_receipt,
    machine_code,
    machine_json_bytes,
    required_delivery_complete,
    results_payload_from_observation,
    verify_available_artifacts,
)
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils.lock import file_lock_at
from orca_auto.core.utils.persistence import (
    atomic_write_text as _atomic_write_text,
)
from orca_auto.core.utils.persistence import (
    durable_mkdir,
    load_json_mapping_file,
    timestamped_token,
)
from orca_auto.core.utils.persistence import (
    now_utc_iso as _now_utc_iso,
)

from .report import write_job_html_report
from .report.si import write_si_block
from .statuses import RunStatus
from .types import RunFinalResult, RunState

logger = logging.getLogger(__name__)


STATE_FILE_NAME = RUN_STATE_FILE
STATE_MUTATION_LOCK_FILE_NAME = ".job_state.mutation.lock"
REPORT_JSON_NAME = RUN_REPORT_JSON_FILE


def now_utc_iso() -> str:
    return _now_utc_iso()


def state_path(reaction_dir: Path) -> Path:
    return reaction_dir / STATE_FILE_NAME


def report_json_path(reaction_dir: Path) -> Path:
    return reaction_dir / REPORT_JSON_NAME


def state_payload_job_id(payload: Any) -> str:
    """Read the generation job id from legacy or normalized ORCA state."""

    if not isinstance(payload, dict):
        return ""
    job = payload.get("job")
    job = job if isinstance(job, dict) else {}
    return str(payload.get("job_id") or job.get("id") or "").strip()


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    return load_json_mapping_file(path)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _selected_input_text(payload: Mapping[str, Any]) -> str:
    selected = _text(payload.get("selected_inp"))
    if selected:
        return selected
    input_payload = payload.get("input")
    if isinstance(input_payload, Mapping):
        return _text(input_payload.get("primary_path"))
    return ""


def _execution_provenance(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    provenance = payload.get("execution_provenance")
    if isinstance(provenance, Mapping):
        return provenance
    engine_payload = payload.get("engine_payload")
    if isinstance(engine_payload, Mapping):
        provenance = engine_payload.get("execution_provenance")
        if isinstance(provenance, Mapping):
            return provenance
    return {}


def _visible_generation_artifact_dir(
    reaction_dir: Path,
    payload: Mapping[str, Any],
) -> tuple[Path, tuple[int, int]] | None:
    selected_text = _selected_input_text(payload)
    provenance = _execution_provenance(payload)
    execution_dir_text = _text(provenance.get("execution_dir"))
    raw_identity = provenance.get("execution_dir_identity")
    bound_selected_identity = provenance.get("bound_selected_identity")
    generation_owner_token = _text(provenance.get("generation_owner_token"))
    if (
        not selected_text
        or not execution_dir_text
        or not generation_owner_token
        or not isinstance(raw_identity, Mapping)
        or not isinstance(bound_selected_identity, Mapping)
    ):
        return None
    try:
        device = int(raw_identity.get("device", -1))
        inode = int(raw_identity.get("inode", -1))
        resolved_reaction_dir = reaction_dir.expanduser().resolve(strict=True)
        raw_generation_dir = Path(execution_dir_text).expanduser()
        generation_dir = raw_generation_dir.resolve(strict=True)
        generation_status = raw_generation_dir.lstat()
        reaction_status = resolved_reaction_dir.stat()
        raw_selected = Path(selected_text).expanduser()
        selected = require_confined_regular_file(
            generation_dir,
            raw_selected,
            label="ORCA generation selected input",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        device < 0
        or inode <= 0
        or not raw_generation_dir.is_absolute()
        or raw_generation_dir != generation_dir
        or raw_generation_dir.is_symlink()
        or generation_dir.parent != resolved_reaction_dir
        or not is_visible_generation_name(generation_dir.name)
        or not generation_dir.is_dir()
        or raw_selected != selected
        or selected.parent != generation_dir
        or _text(bound_selected_identity.get("path")) != str(selected)
        or (int(generation_status.st_dev), int(generation_status.st_ino)) != (device, inode)
    ):
        return None
    try:
        require_direct_generation_owner(
            resolved_reaction_dir,
            namespace=generation_dir.name,
            expected_job_identity=(
                int(reaction_status.st_dev),
                int(reaction_status.st_ino),
            ),
            expected_generation_identity=(device, inode),
            owner_token=generation_owner_token,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return generation_dir, (device, inode)


def _write_generation_json(
    target: tuple[Path, tuple[int, int]],
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    _write_generation_bytes(
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


def _write_generation_bytes(
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


def _state_from_normalized_payload(payload: dict[str, Any]) -> RunState | None:
    if int(payload.get("schema_version", 0) or 0) != 1:
        return None
    if _text(payload.get("engine")) != "orca":
        return None
    job = _dict(payload.get("job"))
    status = _dict(payload.get("status"))
    input_payload = _dict(payload.get("input"))
    timestamps = _dict(payload.get("timestamps"))
    engine_payload = _dict(payload.get("engine_payload"))
    state: RunState = {
        "job_id": _text(job.get("id")),
        "queue_id": _text(job.get("queue_id")),
        "queue_generation": _text(job.get("generation")),
        "run_id": _text(engine_payload.get("run_id")),
        "reaction_dir": _text(job.get("dir")),
        "selected_inp": _text(input_payload.get("primary_path")),
        "max_retries": int(engine_payload.get("max_retries", 0) or 0),
        "status": _text(status.get("state")),
        "started_at": _text(timestamps.get("started_at")),
        "updated_at": _text(timestamps.get("updated_at")),
        "attempts": list(engine_payload.get("attempts") or []),
        "scratch_publications": list(engine_payload.get("scratch_publications") or []),
        "execution_provenance": _dict(engine_payload.get("execution_provenance")),
        "final_result": cast(RunFinalResult | None, engine_payload.get("final_result")),
    }
    return state


def load_state(reaction_dir: Path) -> RunState | None:
    raw = _load_json_dict(state_path(reaction_dir))
    if raw is None:
        return None
    return _state_from_normalized_payload(raw)


def load_generation_state(
    generation_dir: Path,
) -> tuple[dict[str, Any], RunState] | None:
    """Load a state artifact without following links outside its generation."""

    raw_generation_dir = generation_dir.expanduser()
    if (
        not raw_generation_dir.is_absolute()
        or raw_generation_dir.is_symlink()
        or not is_visible_generation_name(raw_generation_dir.name)
    ):
        return None
    try:
        resolved_generation_dir = raw_generation_dir.resolve(strict=True)
        before = resolved_generation_dir.stat()
        if raw_generation_dir != resolved_generation_dir or not stat.S_ISDIR(before.st_mode):
            return None
        payload = json.loads(
            read_confined_text(
                resolved_generation_dir,
                state_path(resolved_generation_dir),
                label="ORCA generation state",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
        )
        after = resolved_generation_dir.stat()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        return None
    normalized = _state_from_normalized_payload(payload)
    if normalized is None:
        return None
    return payload, normalized


def load_report_json(
    generation_dir: Path,
    *,
    require_consumable_success: bool = False,
) -> dict[str, Any] | None:
    """Load one provenance-verified ORCA report from an exact visible generation."""

    raw_generation_dir = generation_dir.expanduser()
    if (
        not raw_generation_dir.is_absolute()
        or raw_generation_dir.is_symlink()
        or not is_visible_generation_name(raw_generation_dir.name)
    ):
        return None
    report_path = report_json_path(raw_generation_dir)
    try:
        resolved_generation_dir = raw_generation_dir.resolve(strict=True)
        generation_before = resolved_generation_dir.stat()
        before = report_path.lstat()
        observation = json.loads(
            read_confined_text(
                resolved_generation_dir,
                report_path,
                label="ORCA generation report",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        raw_generation_dir != resolved_generation_dir
        or not stat.S_ISDIR(generation_before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not isinstance(observation, dict)
    ):
        return None
    result_data = results_payload_from_observation(observation)
    if result_data is None:
        return None
    if (
        observation.get("producer", {}).get("name") != "orca_auto"
        or observation.get("operation", {}).get("kind") != "chemistry/orca-run"
        or result_data.get("result_kind") != "engine-run"
        or result_data.get("engine") != "orca"
        or not verify_available_artifacts(observation, resolved_generation_dir)
    ):
        return None
    loaded_state = load_generation_state(resolved_generation_dir)
    if loaded_state is None:
        return None
    payload, _state = loaded_state
    job = _dict(payload.get("job"))
    status = _dict(payload.get("status"))
    input_payload = _dict(payload.get("input"))
    engine_payload = _dict(payload.get("engine_payload"))
    final_result = _dict(engine_payload.get("final_result"))
    summary = _dict(result_data.get("summary"))
    results = _dict(result_data.get("results"))
    operation_id = _text(observation.get("operation", {}).get("id"))
    expected_phase, expected_outcome = _machine_lifecycle(_text(status.get("state")))
    lifecycle = _dict(observation.get("lifecycle"))
    attempts = engine_payload.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    if (
        operation_id not in {_text(job.get("id")), _text(engine_payload.get("run_id"))}
        or lifecycle.get("phase") != expected_phase
        or lifecycle.get("outcome") != expected_outcome
        or summary.get("status") != _text(status.get("state"))
        or summary.get("reason") != _text(final_result.get("reason") or status.get("reason"))
        or summary.get("analyzer_status") != _text(final_result.get("analyzer_status"))
        or results.get("run_id") != _text(engine_payload.get("run_id"))
        or results.get("attempt_count") != attempt_count
        or results.get("max_retries") != int(engine_payload.get("max_retries", 0) or 0)
        or results.get("resumed") != bool(final_result.get("resumed", False))
        or results.get("skipped_execution") != bool(final_result.get("skipped_execution", False))
        or results.get("runner_error") != _text(final_result.get("runner_error"))
    ):
        return None
    artifacts = observation.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    expected_input = artifact_receipt(
        resolved_generation_dir,
        Path(_text(input_payload.get("primary_path"))),
        required=True,
        role="source",
        media_type="text/plain",
    )
    if artifacts.get("input") != expected_input:
        return None
    outcome = expected_outcome
    last_out_path = _text(final_result.get("last_out_path"))
    if last_out_path or outcome == "succeeded":
        expected_output = artifact_receipt(
            resolved_generation_dir,
            Path(last_out_path) if last_out_path else None,
            required=outcome == "succeeded",
            role="log",
            media_type="text/plain",
        )
        if artifacts.get("orca-output") != expected_output:
            return None
    if require_consumable_success and outcome == "succeeded":
        handoff = _dict(observation.get("handoff"))
        delivery = _dict(observation.get("delivery"))
        if handoff.get("status") != "ready" or delivery.get("status") != "complete":
            return None
    target = _visible_generation_artifact_dir(resolved_generation_dir.parent, payload)
    if target is None or target[0] != resolved_generation_dir:
        return None
    provenance = _execution_provenance(payload)
    bound_selected_identity = provenance.get("bound_selected_identity")
    selected_text = _selected_input_text(payload)
    if not isinstance(bound_selected_identity, Mapping) or not selected_text:
        return None
    try:
        selected = require_confined_regular_file(
            resolved_generation_dir,
            Path(selected_text).expanduser(),
            label="ORCA report selected input",
        )
        if _engine_runner.executable_identity(selected) != dict(bound_selected_identity):
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    try:
        after = report_path.lstat()
        generation_details = resolved_generation_dir.stat()
    except OSError:
        return None
    if (
        (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or (
            int(generation_before.st_dev),
            int(generation_before.st_ino),
        )
        != (
            int(generation_details.st_dev),
            int(generation_details.st_ino),
        )
        or target[1] != (int(generation_details.st_dev), int(generation_details.st_ino))
    ):
        return None
    return payload


def new_state(reaction_dir: Path, selected_inp: Path, max_retries: int) -> RunState:
    run_id = timestamped_token("run", token_bytes=16)
    ts = now_utc_iso()
    return {
        "run_id": run_id,
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "max_retries": int(max_retries),
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
    path = state_path(reaction_dir)
    durable_mkdir(reaction_dir, parents=True, exist_ok=True)
    payload = _normalized_payload_from_state(reaction_dir, state_payload)
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
            generation_target = _visible_generation_artifact_dir(reaction_dir, state_payload)
            if generation_target is not None:
                _write_generation_json(
                    generation_target,
                    state_path(generation_target[0]),
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
    status: str,
    final_result: RunFinalResult,
) -> None:
    state["status"] = status
    state["final_result"] = final_result
    write_state(reaction_dir, state)


def _normalized_payload_from_state(reaction_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    scratch_publications = state.get("scratch_publications")
    if not isinstance(scratch_publications, list):
        scratch_publications = []
    final_result = state.get("final_result")
    final_result_payload = _dict(final_result)
    selected_inp = _text(state.get("selected_inp"))
    status = _text(state.get("status"))
    reason = _text(final_result_payload.get("reason"))
    job_id = _text(state.get("job_id")) or _text(state.get("run_id"))
    last_out_path = _text(final_result_payload.get("last_out_path"))
    updated_at = _text(state.get("updated_at")) or now_utc_iso()
    return build_engine_artifact_payload(
        engine="orca",
        job=EngineArtifactJob(
            id=job_id,
            queue_id=_text(state.get("queue_id")),
            dir=_text(state.get("reaction_dir")) or str(reaction_dir.resolve()),
            app_name="orca_auto_orca",
            task_id=job_id,
            generation=_text(state.get("queue_generation")),
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
            created_at=_text(state.get("started_at")),
            started_at=_text(state.get("started_at")),
            updated_at=updated_at,
            finished_at=_text(final_result_payload.get("completed_at")),
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
            "run_id": _text(state.get("run_id")),
            "max_retries": int(state.get("max_retries", 0) or 0),
            "attempts": attempts,
            "scratch_publications": scratch_publications,
            "execution_provenance": _dict(state.get("execution_provenance")),
            "final_result": final_result,
        },
    )


def _machine_lifecycle(status: str) -> tuple[str, str]:
    normalized = status.strip().lower()
    if normalized in {"created", "pending", "queued"}:
        return "queued", "pending"
    if normalized in {"running", "retrying"}:
        return "running", "pending"
    if normalized == "completed":
        return "finished", "succeeded"
    if normalized == "cancelled":
        return "finished", "cancelled"
    if normalized == "failed":
        return "finished", "failed"
    return "finished", "uncertain"


def _machine_observation(
    generation_dir: Path,
    report_payload: Mapping[str, Any],
    *,
    published_artifacts: Mapping[str, tuple[Path, str, str]] | None = None,
) -> dict[str, Any]:
    job = _dict(report_payload.get("job"))
    status_payload = _dict(report_payload.get("status"))
    input_payload = _dict(report_payload.get("input"))
    engine_payload = _dict(report_payload.get("engine_payload"))
    final_result = _dict(engine_payload.get("final_result"))
    status = _text(status_payload.get("state"))
    reason = _text(final_result.get("reason") or status_payload.get("reason"))
    analyzer_status = _text(final_result.get("analyzer_status"))
    phase, outcome = _machine_lifecycle(status)
    operation_id = _text(job.get("id")) or _text(engine_payload.get("run_id"))

    artifacts: dict[str, dict[str, Any]] = {
        "input": artifact_receipt(
            generation_dir,
            Path(_text(input_payload.get("primary_path")))
            if _text(input_payload.get("primary_path"))
            else None,
            required=True,
            role="source",
            media_type="text/plain",
        )
    }
    last_out_path = _text(final_result.get("last_out_path"))
    if last_out_path or outcome == "succeeded":
        artifacts["orca-output"] = artifact_receipt(
            generation_dir,
            Path(last_out_path) if last_out_path else None,
            required=outcome == "succeeded",
            role="log",
            media_type="text/plain",
        )
    for artifact_id, (path, role, media_type) in sorted((published_artifacts or {}).items()):
        artifacts[artifact_id] = artifact_receipt(
            generation_dir,
            path,
            required=False,
            role=role,
            media_type=media_type,
        )

    complete = required_delivery_complete(artifacts)
    if phase != "finished":
        handoff_status = "pending"
        delivery_status = "pending"
        handoff_codes: list[str] = []
        delivery_codes: list[str] = []
    else:
        delivery_status = "complete" if complete else "incomplete"
        delivery_codes = [] if complete else ["orca_auto/required_artifact_unavailable"]
        if outcome == "succeeded" and complete:
            handoff_status = "ready"
            handoff_codes = []
        else:
            handoff_status = "blocked"
            handoff_codes = [
                machine_code(
                    "orca_auto",
                    reason if outcome != "succeeded" else "required_artifact_unavailable",
                    fallback="operation_not_ready",
                )
            ]
    lifecycle_codes = (
        []
        if outcome in {"pending", "succeeded"}
        else [machine_code("orca_auto", reason or outcome, fallback="operation_failed")]
    )
    attempts = engine_payload.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    result_details = {
        "run_id": _text(engine_payload.get("run_id")),
        "analyzer_status": analyzer_status,
        "reason": reason,
        "attempt_count": attempt_count,
        "max_retries": int(engine_payload.get("max_retries", 0) or 0),
        "resumed": bool(final_result.get("resumed", False)),
        "skipped_execution": bool(final_result.get("skipped_execution", False)),
        "runner_error": _text(final_result.get("runner_error")),
    }
    return {
        "contract": {"name": MACHINE_CONTRACT_NAME, "version": MACHINE_CONTRACT_VERSION},
        "producer": {"name": "orca_auto", "version": __version__},
        "operation": {"id": operation_id, "kind": "chemistry/orca-run"},
        "lifecycle": {"phase": phase, "outcome": outcome, "codes": lifecycle_codes},
        "handoff": {"status": handoff_status, "codes": handoff_codes},
        "delivery": {"status": delivery_status, "codes": delivery_codes},
        "artifacts": artifacts,
        "lineage": {"trace_id": None, "upstream": []},
        "payload": {
            "contract": {
                "name": RESULTS_PAYLOAD_CONTRACT_NAME,
                "version": RESULTS_PAYLOAD_CONTRACT_VERSION,
            },
            "data": {
                "result_kind": "engine-run",
                "engine": "orca",
                "summary": {
                    "status": status,
                    "reason": reason,
                    "analyzer_status": analyzer_status,
                    "attempt_count": attempt_count,
                },
                "results": result_details,
                "artifact_refs": sorted(artifacts),
            },
        },
    }


def _published_terminal_observation(
    generation_dir: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Existing finished observation text and lifecycle, if one is published.

    A missing or nonterminal observation returns ``None``. Any existing path
    that cannot be read as bounded UTF-8 JSON fails closed before artifact
    writers can change files pinned by a terminal observation.
    """
    path = report_json_path(generation_dir)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"existing machine observation is invalid: {path}") from exc
    try:
        existing_text = read_confined_text(
            generation_dir,
            path,
            label="ORCA generation machine observation",
            max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
        )
        existing = json.loads(existing_text)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"existing machine observation is invalid: {path}") from exc
    lifecycle = _dict(existing.get("lifecycle")) if isinstance(existing, dict) else {}
    if lifecycle.get("phase") != "finished":
        return None
    return existing_text, lifecycle


def write_report_json(
    reaction_dir: Path,
    report_payload: dict[str, Any],
    *,
    generation_target: tuple[Path, tuple[int, int]] | None = None,
    published_artifacts: Mapping[str, tuple[Path, str, str]] | None = None,
) -> Path | None:
    if int(report_payload.get("schema_version", 0) or 0) == 1:
        payload = report_payload
    else:
        state: RunState = {
            "job_id": _text(report_payload.get("job_id")),
            "run_id": _text(report_payload.get("run_id")),
            "reaction_dir": _text(report_payload.get("reaction_dir")) or str(reaction_dir),
            "selected_inp": _text(report_payload.get("selected_inp")),
            "max_retries": int(report_payload.get("max_retries", 0) or 0),
            "status": _text(report_payload.get("status")),
            "started_at": _text(report_payload.get("started_at")),
            "updated_at": _text(report_payload.get("updated_at")),
            "attempts": list(report_payload.get("attempts") or []),
            "scratch_publications": list(report_payload.get("scratch_publications") or []),
            "execution_provenance": _dict(report_payload.get("execution_provenance")),
            "final_result": cast(RunFinalResult | None, report_payload.get("final_result")),
        }
        payload = _normalized_payload_from_state(reaction_dir, state)
    if generation_target is None:
        generation_target = _visible_generation_artifact_dir(reaction_dir, payload)
    if generation_target is None:
        logger.warning(
            "report JSON not published: no verified execution generation for %s", reaction_dir
        )
        return None
    observation = _machine_observation(
        generation_target[0],
        payload,
        published_artifacts=published_artifacts,
    )
    path = report_json_path(generation_target[0])
    observation_bytes = machine_json_bytes(observation)
    existing_terminal = _published_terminal_observation(generation_target[0])
    if existing_terminal is not None:
        existing_text, _ = existing_terminal
        if existing_text.encode("utf-8") == observation_bytes:
            return path
        raise RuntimeError(f"terminal machine observation is immutable: {path}")
    _write_generation_bytes(generation_target, path, observation_bytes)
    return path


def _existing_terminal_report_paths(
    generation_dir: Path,
) -> tuple[dict[str, str], str] | None:
    """Published report paths and recorded outcome of a terminal ``machine.json``.

    ``None`` means no terminal observation is published yet and reports may be
    written. An unreadable or corrupt existing observation fails closed.
    """
    published_terminal = _published_terminal_observation(generation_dir)
    if published_terminal is None:
        return None
    _, lifecycle = published_terminal
    path = report_json_path(generation_dir)
    reports = {"report_json": str(path)}
    html_path = generation_dir / RUN_REPORT_HTML_FILE
    if html_path.is_file():
        reports["report_html"] = str(html_path)
    si_path = generation_dir / SI_BLOCK_MD_FILE
    if si_path.is_file():
        reports["si_block"] = str(si_path)
    return reports, _text(lifecycle.get("outcome"))


def write_report_files(reaction_dir: Path, state: Mapping[str, Any]) -> dict[str, str]:
    """Write the machine (JSON) and human (HTML, SI block) job reports.

    Reports are published only inside the verified execution generation. A run
    whose generation cannot be verified gets no report (fail closed, logged);
    its state and queue record still carry the outcome.
    """

    report_payload = _normalized_payload_from_state(reaction_dir, state)
    generation_target = _visible_generation_artifact_dir(reaction_dir, report_payload)
    if generation_target is None:
        logger.warning(
            "job reports not published: no verified execution generation for %s", reaction_dir
        )
        return {}
    existing_terminal = _existing_terminal_report_paths(generation_target[0])
    if existing_terminal is not None:
        # The published terminal machine.json pins its artifacts' exact bytes
        # and is immutable; re-entry must not regenerate or remove any of them.
        existing_reports, existing_outcome = existing_terminal
        current_status = _text(_dict(report_payload.get("status")).get("state"))
        if _machine_lifecycle(current_status)[1] != existing_outcome:
            logger.warning(
                "terminal machine observation outcome %r no longer matches job state %r "
                "for %s; the immutable generation is preserved unchanged",
                existing_outcome,
                current_status,
                generation_target[0],
            )
        return existing_reports
    reports: dict[str, str] = {}
    html_path = write_job_html_report(reaction_dir, state, generation_target=generation_target)
    if html_path is not None:
        reports["report_html"] = str(html_path)
    si_path = write_si_block(reaction_dir, state, generation_target=generation_target)
    if si_path is not None:
        reports["si_block"] = str(si_path)
    published_artifacts: dict[str, tuple[Path, str, str]] = {}
    if html_path is not None:
        published_artifacts["human-report"] = (html_path, "human-report", "text/html")
    if si_path is not None:
        published_artifacts["supporting-information"] = (
            si_path,
            "supporting-information",
            "text/markdown",
        )
    json_path = write_report_json(
        reaction_dir,
        report_payload,
        generation_target=generation_target,
        published_artifacts=published_artifacts,
    )
    reports["report_json"] = str(json_path)
    return reports
