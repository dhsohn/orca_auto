from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .location import JobLocationRecord
from .text import normalize_index_text as normalize_text


@dataclass(frozen=True)
class EngineLocationSpec:
    app_name: str
    job_type_from_payload: Callable[[str], str]
    default_molecule_key: Callable[[Path, str], str]
    payload_kind_key: str
    payload_kind_default: str
    molecule_key_name: str


def resource_dict(max_cores: int, max_memory_gb: int) -> dict[str, int]:
    return {
        "max_cores": max(1, int(max_cores)),
        "max_memory_gb": max(1, int(max_memory_gb)),
    }


def _resolved_existing_path(existing: JobLocationRecord | None, attr: str) -> Path | None:
    value = normalize_text(getattr(existing, attr)) if existing is not None else ""
    return Path(value).expanduser().resolve() if value else None


def _existing_text(existing: JobLocationRecord | None, attr: str) -> str:
    return normalize_text(getattr(existing, attr)) if existing is not None else ""


def _original_run_dir(existing: JobLocationRecord | None, job_dir: Path) -> Path:
    return _resolved_existing_path(existing, "original_run_dir") or job_dir.expanduser().resolve()


def _selected_input_xyz_text(
    existing: JobLocationRecord | None,
    selected_input_xyz: str,
) -> str:
    return normalize_text(selected_input_xyz) or _existing_text(existing, "selected_input_xyz")


def _molecule_key_text(
    existing: JobLocationRecord | None,
    molecule_key: str,
    *,
    original_run_dir: Path,
    selected_input_xyz: str,
    default_molecule_key_fn: Callable[[Path, str], str] | None,
) -> str:
    resolved = normalize_text(molecule_key) or _existing_text(existing, "molecule_key")
    if resolved or default_molecule_key_fn is None:
        return resolved
    return default_molecule_key_fn(original_run_dir, selected_input_xyz)


def _resource_payload(
    provided: dict[str, int] | None,
    existing: JobLocationRecord | None,
    attr: str,
) -> dict[str, int]:
    existing_payload = dict(getattr(existing, attr)) if existing is not None else {}
    return dict(provided or existing_payload)


def build_job_location_record(
    *,
    existing: JobLocationRecord | None = None,
    job_id: str,
    app_name: str,
    job_type: str,
    status: str,
    job_dir: Path,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
    default_molecule_key_fn: Callable[[Path, str], str] | None = None,
) -> JobLocationRecord:
    resolved_job_dir = job_dir.expanduser().resolve()
    original_run_dir = _original_run_dir(existing, resolved_job_dir)
    selected_input_xyz_text = _selected_input_xyz_text(existing, selected_input_xyz)
    molecule_key_text = _molecule_key_text(
        existing,
        molecule_key,
        original_run_dir=original_run_dir,
        selected_input_xyz=selected_input_xyz_text,
        default_molecule_key_fn=default_molecule_key_fn,
    )
    resource_request_text = _resource_payload(
        resource_request,
        existing,
        "resource_request",
    )
    resource_actual_text = (
        _resource_payload(
            resource_actual,
            existing,
            "resource_actual",
        )
        or resource_request_text
    )

    return JobLocationRecord(
        job_id=normalize_text(job_id),
        app_name=app_name,
        job_type=job_type,
        status=normalize_text(status),
        original_run_dir=str(original_run_dir),
        molecule_key=molecule_key_text,
        selected_input_xyz=selected_input_xyz_text,
        latest_known_path=str(resolved_job_dir.resolve()),
        resource_request=resource_request_text,
        resource_actual=resource_actual_text,
    )


def build_engine_job_location_record(
    *,
    spec: EngineLocationSpec,
    existing: JobLocationRecord | None = None,
    job_id: str,
    status: str,
    job_dir: Path,
    payload_kind: str,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
) -> JobLocationRecord:
    return build_job_location_record(
        existing=existing,
        job_id=job_id,
        app_name=spec.app_name,
        job_type=spec.job_type_from_payload(payload_kind),
        status=status,
        job_dir=job_dir,
        selected_input_xyz=selected_input_xyz,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
        default_molecule_key_fn=spec.default_molecule_key,
    )


__all__ = [
    "EngineLocationSpec",
    "build_engine_job_location_record",
    "build_job_location_record",
    "resource_dict",
]
