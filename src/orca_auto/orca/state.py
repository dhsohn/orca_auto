from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import (
    RUN_REPORT_JSON_FILE,
    RUN_REPORT_MD_FILE,
    RUN_STATE_FILE,
)
from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
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
    build_engine_report_markdown,
)
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.core.queue.generation import is_visible_generation_name
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
from .types import RunFinalResult, RunState

logger = logging.getLogger(__name__)


STATE_FILE_NAME = RUN_STATE_FILE
STATE_MUTATION_LOCK_FILE_NAME = ".job_state.mutation.lock"
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
    normalized = _state_from_normalized_payload(raw)
    return normalized


def _load_report_json_unchecked(artifact_dir: Path) -> dict[str, Any] | None:
    payload = _load_json_dict(report_json_path(artifact_dir))
    if payload is None:
        return None
    if int(payload.get("schema_version", 0) or 0) != 1:
        return None
    if _text(payload.get("engine")) != "orca":
        return None
    return payload


def load_report_json(generation_dir: Path) -> dict[str, Any] | None:
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
        before = report_path.lstat()
    except OSError:
        return None
    if (
        raw_generation_dir != resolved_generation_dir
        or not resolved_generation_dir.is_dir()
        or not stat.S_ISREG(before.st_mode)
    ):
        return None
    payload = _load_report_json_unchecked(resolved_generation_dir)
    if payload is None:
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
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or target[1] != (int(generation_details.st_dev), int(generation_details.st_ino)):
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
        "status": "created",
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
            "scratch_publications": scratch_publications,
            "execution_provenance": _dict(state.get("execution_provenance")),
            "final_result": final_result,
        },
    )


def write_report_json(
    reaction_dir: Path,
    report_payload: dict[str, Any],
    *,
    generation_target: tuple[Path, tuple[int, int]] | None = None,
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
    path = report_json_path(generation_target[0])
    _write_generation_json(generation_target, path, payload)
    return path


def write_report_md(
    reaction_dir: Path,
    markdown: str,
    *,
    generation_target: tuple[Path, tuple[int, int]],
) -> Path:
    del reaction_dir
    path = report_md_path(generation_target[0])
    _write_generation_bytes(generation_target, path, markdown.encode("utf-8"))
    return path


def write_report_files(reaction_dir: Path, state: Mapping[str, Any]) -> dict[str, str]:
    """Write the Markdown body before publishing JSON as the report commit marker.

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
    markdown = "\n".join(build_engine_report_markdown(report_payload))
    md_path = write_report_md(reaction_dir, markdown, generation_target=generation_target)
    json_path = write_report_json(reaction_dir, report_payload, generation_target=generation_target)
    reports = {"report_json": str(json_path), "report_md": str(md_path)}
    html_path = write_job_html_report(reaction_dir, state, generation_target=generation_target)
    if html_path is not None:
        reports["report_html"] = str(html_path)
    si_path = write_si_block(reaction_dir, state, generation_target=generation_target)
    if si_path is not None:
        reports["si_block"] = str(si_path)
    return reports
