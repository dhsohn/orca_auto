from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.paths import (
    first_existing_named_file,
    iter_existing_dirs,
    recent_file_candidates,
    resolved_path_text,
)
from orca_auto.core.queue.metadata import (
    mapping_metadata,
    mapping_metadata_value,
)
from orca_auto.core.statuses import TERMINAL_STATUSES as TERMINAL_STATUSES
from orca_auto.core.utils import normalize_bool, normalize_text, safe_int
from orca_auto.core.utils.persistence import load_json_mapping_list_file

from ..input_artifacts import derive_selected_input_xyz as _derive_selected_input_xyz
from ._generation import current_generation_payloads

QUEUE_FILE_NAME = "queue.json"
INDEX_DIR_NAME = "index"


def normalize_path_text(value: Any) -> str:
    raw = normalize_text(value)
    if not raw:
        return ""
    try:
        candidate = Path(raw).expanduser()
    except OSError:
        return raw
    try:
        return str(candidate.resolve())
    except OSError:
        return str(candidate)


def resource_dict_from_any(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw in value.items():
        key_text = normalize_text(key)
        if not key_text:
            continue
        try:
            result[key_text] = int(raw)
        except (TypeError, ValueError):
            continue
    return result


def queue_entry_metadata(queue_entry: dict[str, Any] | None) -> dict[str, Any]:
    return mapping_metadata(queue_entry)


def queue_entry_metadata_value(queue_entry: dict[str, Any] | None, key: str) -> Any:
    return mapping_metadata_value(queue_entry, key)


def resolve_artifact_path(path_value: Any, base_dir: Path | None) -> str:
    raw = normalize_text(path_value)
    if not raw:
        return ""
    try:
        candidate = Path(raw).expanduser()
    except OSError:
        return raw
    if candidate.is_absolute():
        try:
            return str(candidate.resolve())
        except OSError:
            return str(candidate)
    if base_dir is None:
        return raw
    try:
        return str((base_dir / candidate).resolve())
    except OSError:
        return str(base_dir / candidate)


def resolve_existing_path(value: Any) -> Path | None:
    raw = normalize_text(value)
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError:
        return None
    return resolved if resolved.exists() else None


def resolve_existing_job_dir(value: Any) -> Path | None:
    raw = normalize_text(value)
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    return resolved.parent if resolved.is_file() else resolved


def derive_selected_input_xyz(selected_inp: str) -> str:
    return _derive_selected_input_xyz(selected_inp)


def path_or_parent(value: Any) -> Path | None:
    resolved = resolve_existing_path(value)
    if resolved is None:
        return None
    return resolved.parent if not resolved.is_dir() else resolved


def preferred_xyz_names(*paths: Path | None) -> list[str]:
    return [f"{path.stem}.xyz" for path in paths if path is not None and not path.is_dir()]


def prefer_orca_optimized_xyz(
    *,
    selected_inp: str,
    selected_input_xyz: str,
    current_dir: Path | None,
    latest_known_path: str,
    last_out_path: str,
) -> str:
    selected_inp_path = resolve_existing_path(selected_inp)
    selected_input_xyz_path = resolve_existing_path(selected_input_xyz)
    last_out = resolve_existing_path(last_out_path)

    search_dirs = iter_existing_dirs(
        selected_inp_path.parent
        if selected_inp_path is not None and not selected_inp_path.is_dir()
        else None,
        current_dir,
        path_or_parent(latest_known_path),
        last_out.parent if last_out is not None and not last_out.is_dir() else None,
    )
    preferred_match = first_existing_named_file(
        search_dirs, preferred_xyz_names(selected_inp_path, last_out)
    )
    if preferred_match:
        return preferred_match

    source_input = None
    if selected_input_xyz_path is not None and not selected_input_xyz_path.is_dir():
        try:
            source_input = selected_input_xyz_path.resolve()
        except OSError:
            source_input = selected_input_xyz_path

    xyz_candidates = recent_file_candidates(search_dirs, suffix=".xyz", exclude=source_input)
    if not xyz_candidates:
        return ""
    return resolved_path_text(xyz_candidates[0])


def attempt_count(state: dict[str, Any], report: dict[str, Any]) -> int:
    report_count = safe_int(report.get("attempt_count"), default=-1)
    if report_count >= 0:
        return report_count
    attempts = state.get("attempts")
    if isinstance(attempts, list):
        return len(attempts)
    return 0


def max_retries(state: dict[str, Any], report: dict[str, Any]) -> int:
    report_value = safe_int(report.get("max_retries"), default=-1)
    if report_value >= 0:
        return report_value
    return safe_int(state.get("max_retries"), default=0)


def coerce_attempts(state: dict[str, Any], report: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_attempts = report.get("attempts")
    if not isinstance(raw_attempts, list):
        raw_attempts = state.get("attempts")
    if not isinstance(raw_attempts, list):
        return ()

    attempts: list[dict[str, Any]] = []
    for raw in raw_attempts:
        if not isinstance(raw, dict):
            continue
        index = safe_int(raw.get("index"), default=0)
        attempt_number = max(0, index - 1) if index > 0 else 0
        attempts.append(
            {
                "index": index,
                "attempt_number": attempt_number,
                "inp_path": normalize_text(raw.get("inp_path")),
                "out_path": normalize_text(raw.get("out_path")),
                "return_code": safe_int(raw.get("return_code"), default=0),
                "analyzer_status": normalize_text(raw.get("analyzer_status")),
                "analyzer_reason": normalize_text(raw.get("analyzer_reason")),
                "markers": _coerce_markers(raw.get("markers")),
                "patch_actions": list(raw["patch_actions"])
                if isinstance(raw.get("patch_actions"), list)
                else [],
                "started_at": normalize_text(raw.get("started_at")),
                "ended_at": normalize_text(raw.get("ended_at")),
            }
        )
    return tuple(attempts)


def _coerce_markers(value: Any) -> dict[str, Any] | list[Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, list):
        return list(value)
    return []


def final_result_payload(state: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    payload = report.get("final_result")
    if not isinstance(payload, dict):
        payload = state.get("final_result")
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items()}


def status_from_payloads(
    *,
    queue_entry: dict[str, Any] | None,
    state: dict[str, Any],
    report: dict[str, Any],
) -> tuple[str, str, str, str]:
    state, report = current_generation_payloads(queue_entry, state, report)
    queue_status = normalize_text((queue_entry or {}).get("status")).lower()
    cancel_requested = normalize_bool((queue_entry or {}).get("cancel_requested"))

    state_status = normalize_text(state.get("status")).lower()
    report_status = normalize_text(report.get("status")).lower()
    final = final_result_payload(state, report)
    final_status = normalize_text(final.get("status")).lower()
    analyzer_status = normalize_text(final.get("analyzer_status"))
    reason = normalize_text(final.get("reason"))
    completed_at = normalize_text(final.get("completed_at"))

    if final_status in {"completed", "failed"}:
        return final_status, analyzer_status, reason, completed_at
    if queue_status == "cancelled":
        return "cancelled", analyzer_status, reason or "cancelled", completed_at
    if queue_status == "running" and cancel_requested:
        return "cancel_requested", analyzer_status, reason, completed_at
    queue_aliases = {
        "pending": "queued",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
    }
    if queue_status in queue_aliases:
        return queue_aliases[queue_status], analyzer_status, reason, completed_at
    if state_status in {"completed", "failed"}:
        return state_status, analyzer_status, reason, completed_at
    if state_status in {"created", "running", "retrying"}:
        return "running", analyzer_status, reason, completed_at
    if report_status in {"completed", "failed"}:
        return report_status, analyzer_status, reason, completed_at
    if queue_status:
        return queue_status, analyzer_status, reason, completed_at
    if state_status:
        return state_status, analyzer_status, reason, completed_at
    return "unknown", analyzer_status, reason, completed_at


def load_json_list(path: Path) -> list[dict[str, Any]]:
    return load_json_mapping_list_file(path)
