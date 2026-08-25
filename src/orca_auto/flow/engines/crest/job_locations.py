from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.indexing import engines as _engine_locations
from orca_auto.core.indexing.engine_job_locations import EngineJobLocations

from .state import load_state


def job_type_for_mode(mode: str) -> str:
    normalized = _engine_locations.normalize_text(mode).lower()
    return (
        "crest_nci_conformer_search" if normalized == "nci" else "crest_standard_conformer_search"
    )


def normalize_molecule_key(value: str) -> str:
    return _engine_locations.normalize_identifier(value, default="unknown_molecule")


def molecule_key_from_selected_xyz(selected_input_xyz: str, job_dir: Path) -> str:
    raw = _engine_locations.normalize_text(selected_input_xyz)
    source = Path(raw).name if raw else job_dir.name
    stem = Path(source).stem or job_dir.name
    return normalize_molecule_key(stem)


_LOCATION_EXPORTS = EngineJobLocations(
    engine="crest",
    spec=_engine_locations.EngineLocationSpec(
        app_name="orca_auto_crest",
        job_type_from_payload=job_type_for_mode,
        default_molecule_key=lambda original_run_dir, selected: molecule_key_from_selected_xyz(
            selected,
            original_run_dir,
        ),
        payload_kind_key="mode",
        payload_kind_default="standard",
        molecule_key_name="molecule_key",
    ),
    load_state_fn=load_state,
    load_report_json_fn=None,
    payload_kind_kwarg="mode",
    molecule_key_kwarg="molecule_key",
    default_payload_kind_kwarg="default_mode",
)

index_root_for_cfg = _LOCATION_EXPORTS.index_root_for_cfg
runtime_roots_for_cfg = _LOCATION_EXPORTS.runtime_roots_for_cfg
index_root_for_path = _LOCATION_EXPORTS.index_root_for_path
list_job_records_for_cfg = _LOCATION_EXPORTS.list_job_records_for_cfg
resolve_job_location_for_cfg = _LOCATION_EXPORTS.resolve_job_location_for_cfg
build_job_location_record = _LOCATION_EXPORTS.build_job_location_record
upsert_job_record = _LOCATION_EXPORTS.upsert_job_record
resolve_latest_job_dir = _LOCATION_EXPORTS.resolve_latest_job_dir


def record_from_artifacts(*, job_dir: Path, state: dict[str, Any], **kwargs: Any) -> Any:
    return _LOCATION_EXPORTS.record_from_artifacts(
        job_dir=job_dir,
        state=state,
        report=None,
        **kwargs,
    )
