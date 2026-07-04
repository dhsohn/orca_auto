from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord


@dataclass(frozen=True)
class LoadedArtifactFiles:
    job_dir: Path
    record: JobLocationRecord | None
    report: dict[str, Any]
    state: dict[str, Any]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ContractArtifactBundle:
    job_dir: Path
    record: JobLocationRecord | None
    payload: dict[str, Any]
    latest_known_path: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]


@dataclass(frozen=True)
class ContractFieldReader:
    bundle: ContractArtifactBundle

    @property
    def job_dir(self) -> Path:
        return self.bundle.job_dir

    @property
    def record(self) -> JobLocationRecord | None:
        return self.bundle.record

    @property
    def payload(self) -> dict[str, Any]:
        return self.bundle.payload

    def record_value(self, attr: str) -> Any:
        return getattr(self.record, attr) if self.record is not None else ""

    def payload_sequence(self, key: str) -> tuple[str, ...]:
        return normalized_text_sequence(self.payload.get(key))

    def payload_record_text(
        self,
        payload_key: str,
        record_attr: str,
        *,
        default: str = "",
    ) -> str:
        return first_normalized_text(
            self.payload.get(payload_key),
            self.record_value(record_attr),
            default=default,
        )

    def artifact_roots(self, *values: Any) -> tuple[Path, ...]:
        return artifact_roots(self.job_dir, *values)

    def resolved_path(self, value: Any, *, roots: tuple[Path, ...]) -> str:
        return resolve_artifact_path(value, roots=roots)

    def resolved_paths(self, values: Iterable[Any], *, roots: tuple[Path, ...]) -> tuple[str, ...]:
        return tuple(path for value in values if (path := self.resolved_path(value, roots=roots)))


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_scalar_text(value: Any) -> str:
    if isinstance(value, str | int | float | bool):
        return normalize_text(value)
    return ""


def normalized_text_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(text for item in value if (text := normalize_scalar_text(item)))


def first_normalized_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = normalize_scalar_text(value)
        if text:
            return text
    return default


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"artifact JSON cannot be read: {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"artifact file is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"artifact file must contain a JSON object: {path}")
    return raw


def direct_dir_target(
    target: str,
    *,
    path_factory: Callable[[str], Any] = Path,
) -> Path | None:
    raw = normalize_text(target)
    if not raw:
        return None
    try:
        candidate = path_factory(raw).expanduser().resolve()
    except OSError:
        return None
    return candidate if candidate.exists() and candidate.is_dir() else None


def resolved_dir_candidates(
    values: Iterable[Any],
    *,
    path_factory: Callable[[str], Any] = Path,
) -> list[Path]:
    candidates: list[Path] = []
    for value in values:
        raw = normalize_text(value)
        if not raw:
            continue
        try:
            candidates.append(path_factory(raw).expanduser().resolve())
        except OSError:
            continue
    return candidates


def resolve_indexed_job_dir(
    index_root: Path,
    target: str,
    *,
    resolve_job_location_fn: Callable[[Path, str], JobLocationRecord | None],
    direct_path_target_fn: Callable[[str], Path | None],
    missing_label: str,
    path_factory: Callable[[str], Any] = Path,
) -> tuple[Path, JobLocationRecord | None]:
    record = resolve_job_location_fn(index_root, target)
    candidates: list[Path] = []
    if record is not None:
        candidates.extend(
            resolved_dir_candidates(
                (
                    record.latest_known_path,
                    record.original_run_dir,
                ),
                path_factory=path_factory,
            )
        )
    direct = direct_path_target_fn(target)
    if direct is not None:
        candidates.append(direct)

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate, record
    raise FileNotFoundError(f"{missing_label} job directory not found for target: {target}")


def load_artifact_files(
    *,
    job_dir: Path,
    record: JobLocationRecord | None,
    load_json_dict_fn: Callable[[Path], dict[str, Any]],
    report_filename: str,
    state_filename: str,
    missing_label: str,
    select_payload_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> LoadedArtifactFiles:
    report = load_json_dict_fn(job_dir / report_filename)
    state = load_json_dict_fn(job_dir / state_filename)
    payload = select_payload_fn(report, state) if select_payload_fn is not None else report or state
    payload = flatten_engine_artifact_payload(payload)
    if not payload:
        raise FileNotFoundError(
            f"{missing_label} artifact files not found in job directory: {job_dir}"
        )
    return LoadedArtifactFiles(
        job_dir=job_dir,
        record=record,
        report=report,
        state=state,
        payload=payload,
    )


def select_active_artifact_payload(
    report: dict[str, Any],
    state: dict[str, Any],
    *,
    active_statuses: set[str] | frozenset[str],
) -> dict[str, Any]:
    state_payload = flatten_engine_artifact_payload(state)
    report_payload = flatten_engine_artifact_payload(report)
    state_status = normalize_text(state_payload.get("status")).lower()
    if state and state_status in active_statuses:
        return state

    report_job_id = normalize_text(report_payload.get("job_id"))
    state_job_id = normalize_text(state_payload.get("job_id"))
    if state and report_job_id and state_job_id and report_job_id != state_job_id:
        return state

    return report or state


def flatten_engine_artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if int(payload.get("schema_version", 0) or 0) != 1:
        return {}
    job = _mapping(payload.get("job"))
    status = _mapping(payload.get("status"))
    input_payload = _mapping(payload.get("input"))
    resources = _mapping(payload.get("resources"))
    artifacts = _mapping(payload.get("artifacts"))
    engine_payload = _mapping(payload.get("engine_payload"))
    flattened = dict(engine_payload)
    flattened.setdefault("job_id", normalize_text(job.get("id")))
    flattened.setdefault("queue_id", normalize_text(job.get("queue_id")))
    flattened.setdefault("job_dir", normalize_text(job.get("dir")))
    flattened.setdefault("status", normalize_text(status.get("state")))
    flattened.setdefault("reason", normalize_text(status.get("reason")))
    flattened.setdefault("exit_code", status.get("exit_code"))
    flattened.setdefault(
        "selected_input_xyz", normalize_text(input_payload.get("selected_xyz_path"))
    )
    flattened.setdefault("manifest_path", normalize_text(artifacts.get("manifest_path")))
    flattened.setdefault("stdout_log", normalize_text(artifacts.get("stdout_log")))
    flattened.setdefault("stderr_log", normalize_text(artifacts.get("stderr_log")))
    flattened.setdefault(
        "resource_request", resources.get("request") if isinstance(resources, dict) else {}
    )
    flattened.setdefault(
        "resource_actual", resources.get("actual") if isinstance(resources, dict) else {}
    )
    return flattened


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def validate_record_app(
    record: JobLocationRecord | None,
    expected_app_name: str,
    *,
    label: str,
) -> None:
    if record is not None and record.app_name and record.app_name != expected_app_name:
        raise ValueError(
            f"Expected {expected_app_name} index record for {label}, got: {record.app_name}"
        )


def latest_known_path(record: JobLocationRecord | None, job_dir: Path) -> str:
    return normalize_text((record.latest_known_path if record is not None else "") or str(job_dir))


def artifact_roots(
    job_dir: Path,
    *values: Any,
    path_factory: Callable[[str], Any] = Path,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for candidate in (*values, str(job_dir)):
        text = normalize_scalar_text(candidate)
        if not text:
            continue
        try:
            resolved = path_factory(text).expanduser().resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def resolve_artifact_path(
    value: Any,
    *,
    roots: tuple[Path, ...],
    path_factory: Callable[[str], Any] = Path,
) -> str:
    text = normalize_scalar_text(value)
    if not text:
        return ""
    try:
        resolved = path_factory(text).expanduser().resolve()
    except OSError:
        return text
    if resolved.exists():
        return str(resolved)
    for root in roots:
        remapped = root / resolved.name
        if remapped.exists():
            return str(remapped.resolve())
    return str(resolved)


def load_contract_artifact_bundle(
    *,
    index_root: str | Path,
    target: str,
    resolve_job_location_fn: Callable[[Path, str], JobLocationRecord | None],
    load_json_dict_fn: Callable[[Path], dict[str, Any]],
    report_filename: str,
    state_filename: str,
    missing_label: str,
    expected_app_name: str,
    coerce_resource_dict_fn: Callable[[Any], dict[str, int]],
    select_payload_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    path_factory: Callable[[str], Any] = Path,
) -> ContractArtifactBundle:
    resolved_index_root = Path(index_root).expanduser().resolve()
    job_dir, record = resolve_indexed_job_dir(
        resolved_index_root,
        target,
        resolve_job_location_fn=resolve_job_location_fn,
        direct_path_target_fn=lambda raw: direct_dir_target(raw, path_factory=path_factory),
        missing_label=missing_label,
        path_factory=path_factory,
    )
    loaded = load_artifact_files(
        job_dir=job_dir,
        record=record,
        load_json_dict_fn=load_json_dict_fn,
        report_filename=report_filename,
        state_filename=state_filename,
        missing_label=missing_label,
        select_payload_fn=select_payload_fn,
    )
    validate_record_app(record, expected_app_name, label=missing_label)
    resource_request = coerce_resource_dict_fn(
        loaded.payload.get("resource_request")
    ) or coerce_resource_dict_fn(record.resource_request if record is not None else {})
    resource_actual = (
        coerce_resource_dict_fn(loaded.payload.get("resource_actual"))
        or coerce_resource_dict_fn(record.resource_actual if record is not None else {})
        or dict(resource_request)
    )
    return ContractArtifactBundle(
        job_dir=job_dir,
        record=record,
        payload=loaded.payload,
        latest_known_path=latest_known_path(record, job_dir),
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
