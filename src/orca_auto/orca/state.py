from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from orca_auto.core.artifacts import (
    RUN_REPORT_JSON_FILE,
    RUN_REPORT_MD_FILE,
    RUN_STATE_FILE,
)
from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
    build_engine_report_markdown,
)
from orca_auto.core.utils.persistence import (
    atomic_write_json,
    load_json_mapping_file,
    timestamped_token,
)
from orca_auto.core.utils.persistence import (
    atomic_write_text as _atomic_write_text,
)
from orca_auto.core.utils.persistence import (
    now_utc_iso as _now_utc_iso,
)

from .report import write_job_html_report
from .types import RunFinalResult, RunState

logger = logging.getLogger(__name__)


STATE_FILE_NAME = RUN_STATE_FILE
REPORT_JSON_NAME = RUN_REPORT_JSON_FILE
REPORT_MD_NAME = RUN_REPORT_MD_FILE


def now_utc_iso() -> str:
    return _now_utc_iso()


def state_path(reaction_dir: Path) -> Path:
    return reaction_dir / STATE_FILE_NAME


def report_json_path(reaction_dir: Path) -> Path:
    return reaction_dir / REPORT_JSON_NAME


def report_md_path(reaction_dir: Path) -> Path:
    return reaction_dir / REPORT_MD_NAME


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    return load_json_mapping_file(path)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


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
        "run_id": _text(engine_payload.get("run_id")),
        "reaction_dir": _text(job.get("dir")),
        "selected_inp": _text(input_payload.get("primary_path")),
        "max_retries": int(engine_payload.get("max_retries", 0) or 0),
        "status": _text(status.get("state")),
        "started_at": _text(timestamps.get("started_at")),
        "updated_at": _text(timestamps.get("updated_at")),
        "attempts": list(engine_payload.get("attempts") or []),
        "final_result": cast(RunFinalResult | None, engine_payload.get("final_result")),
    }
    return state


def load_state(reaction_dir: Path) -> RunState | None:
    raw = _load_json_dict(state_path(reaction_dir))
    if raw is None:
        return None
    normalized = _state_from_normalized_payload(raw)
    return normalized


def load_report_json(reaction_dir: Path) -> dict[str, Any] | None:
    payload = _load_json_dict(report_json_path(reaction_dir))
    if payload is None:
        return None
    if int(payload.get("schema_version", 0) or 0) != 1:
        return None
    if _text(payload.get("engine")) != "orca":
        return None
    return payload


def new_state(reaction_dir: Path, selected_inp: Path, max_retries: int) -> RunState:
    run_id = timestamped_token("run", token_bytes=4)
    ts = now_utc_iso()
    return {
        "run_id": run_id,
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "max_retries": int(max_retries),
        "status": "created",
        "started_at": ts,
        "updated_at": ts,
        "attempts": [],
        "final_result": None,
    }


atomic_write_text = _atomic_write_text


def write_state(reaction_dir: Path, state: Mapping[str, Any]) -> Path:
    state_payload = dict(state)
    state_payload["updated_at"] = now_utc_iso()
    path = state_path(reaction_dir)
    atomic_write_json(
        path,
        _normalized_payload_from_state(reaction_dir, state_payload),
        ensure_ascii=True,
        indent=2,
    )
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
            queue_id="",
            dir=_text(state.get("reaction_dir")) or str(reaction_dir.resolve()),
            app_name="orca_auto_orca",
            task_id=job_id,
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
            "final_result": final_result,
        },
    )


def write_report_json(reaction_dir: Path, report_payload: dict[str, Any]) -> Path:
    path = report_json_path(reaction_dir)
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
            "final_result": cast(RunFinalResult | None, report_payload.get("final_result")),
        }
        payload = _normalized_payload_from_state(reaction_dir, state)
    atomic_write_json(path, payload, ensure_ascii=True, indent=2)
    return path


def write_report_md(reaction_dir: Path, markdown: str) -> Path:
    path = report_md_path(reaction_dir)
    _atomic_write_text(path, markdown)
    return path


def write_report_files(reaction_dir: Path, state: Mapping[str, Any]) -> dict[str, str]:
    report_payload = _normalized_payload_from_state(reaction_dir, state)
    json_path = write_report_json(reaction_dir, report_payload)
    md_path = write_report_md(
        reaction_dir,
        "\n".join(build_engine_report_markdown(report_payload)),
    )
    reports = {"report_json": str(json_path), "report_md": str(md_path)}
    html_path = write_job_html_report(reaction_dir, state)
    if html_path is not None:
        reports["report_html"] = str(html_path)
    return reports
