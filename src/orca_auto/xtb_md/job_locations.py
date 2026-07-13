from __future__ import annotations

from pathlib import Path

from orca_auto.core.indexing import engines as _engine_locations
from orca_auto.core.indexing.engine_job_locations import (
    build_store_backed_engine_job_location_exports,
)

from .state import load_report_json, load_state


def job_type_for_ensemble(ensemble: str) -> str:
    normalized = _engine_locations.normalize_text(ensemble).lower()
    return f"xtb_md_{normalized}" if normalized in {"nvt", "nve"} else "xtb_md"


def molecule_key_from_selected_xyz(selected_input_xyz: str, job_dir: Path) -> str:
    raw = _engine_locations.normalize_text(selected_input_xyz)
    source = Path(raw).name if raw else job_dir.name
    return _engine_locations.normalize_identifier(
        Path(source).stem or job_dir.name,
        default="unknown_molecule",
    )


_EXPORTS = build_store_backed_engine_job_location_exports(
    engine="xtb_md",
    spec=_engine_locations.EngineLocationSpec(
        app_name="orca_auto_xtb_md",
        job_type_from_payload=job_type_for_ensemble,
        default_molecule_key=lambda original_run_dir, selected: molecule_key_from_selected_xyz(
            selected, original_run_dir
        ),
        payload_kind_key="ensemble",
        payload_kind_default="nvt",
        molecule_key_name="molecule_key",
    ),
    load_state_fn=load_state,
    load_report_json_fn=load_report_json,
    payload_kind_kwarg="ensemble",
    molecule_key_kwarg="molecule_key",
    default_payload_kind_kwarg="default_ensemble",
)

index_root_for_cfg = _EXPORTS.index_root_for_cfg
runtime_roots_for_cfg = _EXPORTS.runtime_roots_for_cfg
index_root_for_path = _EXPORTS.index_root_for_path
list_job_records_for_cfg = _EXPORTS.list_job_records_for_cfg
resolve_job_location_for_cfg = _EXPORTS.resolve_job_location_for_cfg
build_job_location_record = _EXPORTS.build_job_location_record
upsert_job_record = _EXPORTS.upsert_job_record
resolve_latest_job_dir = _EXPORTS.resolve_latest_job_dir
load_job_artifacts = _EXPORTS.load_job_artifacts
load_job_artifacts_for_cfg = _EXPORTS.load_job_artifacts_for_cfg
record_from_artifacts = _EXPORTS.record_from_artifacts


__all__ = [
    "index_root_for_cfg",
    "index_root_for_path",
    "job_type_for_ensemble",
    "list_job_records_for_cfg",
    "load_job_artifacts",
    "load_job_artifacts_for_cfg",
    "molecule_key_from_selected_xyz",
    "record_from_artifacts",
    "resolve_job_location_for_cfg",
    "resolve_latest_job_dir",
    "runtime_roots_for_cfg",
    "upsert_job_record",
]
