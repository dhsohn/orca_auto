"""Build and upsert ORCA rows of the job-location index.

``build_job_location_record`` assembles one row with the ORCA conventions (app
name, ``orca_``-prefixed job type, molecule key derived from the selected
input) on top of an existing row, and ``upsert_job_record`` writes such a row
for one job id under the index root. ``resolve_job_metadata`` derives the job
type and molecule key from a selected input; submission and the artifact
projection share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.indexing import (
    JobLocationRecord,
    get_job_location,
    list_job_locations,
    upsert_job_location,
)
from orca_auto.core.indexing.store import normalize_index_text
from orca_auto.core.utils import normalize_text
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_APP_NAME

from ..config import AppConfig
from ..input_artifacts import xyzfile_input_path
from ..input_blocks import parse_input
from ..input_syntax import read_input_text
from ..job_type import job_type_from_routes
from ..molecule_key import directory_fallback_key, molecule_key_from_text
from ._utils import normalize_path_text


def index_root_for_cfg(cfg: AppConfig) -> Path:
    return Path(cfg.runtime.allowed_root).expanduser().resolve()


def job_type_identifier(job_type: str) -> str:
    normalized = normalize_text(job_type).lower()
    if normalized.startswith("orca_"):
        return normalized
    return f"orca_{normalized or 'other'}"


@dataclass(frozen=True)
class InputMetadata:
    """What a selected input implies for a job-location row; ``""`` for a field not asked for."""

    selected_input_xyz: str = ""
    job_type: str = ""
    molecule_key: str = ""


def derive_input_metadata(
    selected_inp: str,
    job_dir: Path,
    *,
    need_selected_input_xyz: bool = False,
    need_job_type: bool = True,
    need_molecule_key: bool = True,
) -> InputMetadata:
    """The metadata a selected input implies, from at most one read of it.

    Only the requested fields are derived, so a caller whose artifacts
    already carry them never opens the input. Otherwise the input is read
    once, through the stable reader (a FIFO, a non-regular file or one
    changed while read is no input), and one parse serves the ``xyzfile``
    path, the route type and the molecule key. The geometry file is read only
    when the key needs its formula. An unusable or missing input gives the
    fallbacks: no geometry path, the ``other`` type, and the directory key
    of ``job_dir``, the job's own directory, as submission recorded it.
    """
    if not (need_selected_input_xyz or need_job_type or need_molecule_key):
        return InputMetadata()
    raw = normalize_text(selected_inp)
    resolved: Path | None = None
    if raw:
        try:
            resolved = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            resolved = None
    text = read_input_text(resolved) if resolved is not None else None
    if resolved is None or text is None:
        return InputMetadata(
            job_type="other" if need_job_type else "",
            molecule_key=directory_fallback_key(job_dir) if need_molecule_key else "",
        )
    parsed = parse_input(text.splitlines())
    return InputMetadata(
        selected_input_xyz=(
            xyzfile_input_path(parsed, resolved.parent) if need_selected_input_xyz else ""
        ),
        job_type=job_type_from_routes(list(parsed.route_lines)) if need_job_type else "",
        molecule_key=(
            molecule_key_from_text(text, resolved, parsed=parsed, fallback_dir=job_dir).key
            if need_molecule_key
            else ""
        ),
    )


def resolve_job_metadata(
    selected_inp: str,
    job_dir: Path,
    *,
    need_job_type: bool = True,
    need_molecule_key: bool = True,
) -> tuple[str, str]:
    """The job type and molecule key a selected input implies (:func:`derive_input_metadata`)."""
    metadata = derive_input_metadata(
        selected_inp,
        job_dir,
        need_job_type=need_job_type,
        need_molecule_key=need_molecule_key,
    )
    return metadata.job_type, metadata.molecule_key


def resource_dict(max_cores: int, max_memory_gb: int) -> dict[str, int]:
    return {
        "max_cores": max(1, int(max_cores)),
        "max_memory_gb": max(1, int(max_memory_gb)),
    }


def _existing_text(existing: JobLocationRecord | None, attr: str) -> str:
    return normalize_index_text(getattr(existing, attr)) if existing is not None else ""


def _existing_resources(
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
    status: str,
    job_dir: Path,
    job_type: str,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
) -> JobLocationRecord:
    """One ORCA row, keeping what ``existing`` already knows where no value is given.

    The original run directory is the existing row's, else ``job_dir``;
    ``latest_known_path`` is always ``job_dir``. An empty molecule key falls
    back to the existing row's, then to the run directory's name. Assembly
    reads no file; callers derive input metadata first
    (:func:`derive_input_metadata`).
    """
    resolved_job_dir = job_dir.expanduser().resolve()
    existing_run_dir = _existing_text(existing, "original_run_dir")
    original_run_dir = (
        Path(existing_run_dir).expanduser().resolve() if existing_run_dir else resolved_job_dir
    )
    selected_input_xyz_text = normalize_index_text(
        normalize_path_text(selected_input_xyz)
    ) or _existing_text(existing, "selected_input_xyz")
    molecule_key_text = normalize_index_text(molecule_key) or _existing_text(
        existing, "molecule_key"
    )
    if not molecule_key_text:
        # Assembly reads no file: a row given no key and holding none falls
        # back to its run directory's name, the directory fallback rule.
        molecule_key_text = directory_fallback_key(original_run_dir)
    resource_request_payload = _existing_resources(resource_request, existing, "resource_request")
    resource_actual_payload = (
        _existing_resources(resource_actual, existing, "resource_actual")
        or resource_request_payload
    )
    return JobLocationRecord(
        job_id=normalize_index_text(job_id),
        app_name=ORCA_AUTO_ORCA_APP_NAME,
        job_type=job_type_identifier(job_type),
        status=normalize_index_text(status or "unknown"),
        original_run_dir=str(original_run_dir),
        molecule_key=molecule_key_text,
        selected_input_xyz=selected_input_xyz_text,
        latest_known_path=str(resolved_job_dir),
        resource_request=resource_request_payload,
        resource_actual=resource_actual_payload,
    )


def upsert_job_record(
    cfg: AppConfig,
    *,
    job_id: str,
    status: str,
    job_dir: Path,
    job_type: str,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
) -> JobLocationRecord:
    root = index_root_for_cfg(cfg)
    existing = get_job_location(root, job_id)
    record = build_job_location_record(
        existing=existing,
        job_id=job_id,
        status=status,
        job_dir=job_dir,
        job_type=job_type,
        selected_input_xyz=selected_input_xyz,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
    return upsert_job_location(root, record)


def list_job_location_records(index_root: str | Path) -> list[JobLocationRecord]:
    return list(list_job_locations(index_root))


def resolve_record_job_dir(record: JobLocationRecord) -> Path | None:
    for value in (record.latest_known_path, record.original_run_dir):
        raw = normalize_text(value)
        if not raw:
            continue
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_dir():
            return resolved
    return None
