from __future__ import annotations

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.indexing import (
    JobLocationRecord,
    get_job_location,
    list_job_locations,
    resolve_job_location,
    upsert_job_location,
)
from orca_auto.core.paths import (
    first_existing_named_file,
    iter_existing_dirs,
    recent_file_candidates,
    resolved_path_text,
    safe_is_subpath,
)

from ..config import AppConfig
from ..job_type import detect_job_type
from ..molecule_key import resolve_molecule_key
from ..state import (
    REPORT_JSON_NAME,
    REPORT_MD_NAME,
    STATE_FILE_NAME,
    load_report_json,
    load_state,
)
from ._contracts import (
    JobArtifactContext,
    JobRuntimeContext,
    load_job_artifact_context,
    load_job_artifacts,
    load_job_runtime_context,
    load_orca_contract_payload,
    resolve_latest_job_dir,
)
from ._records import (
    build_job_location_record,
    collect_reindex_payload,
    index_root_for_cfg,
    is_terminal_status,
    job_type_identifier,
    list_job_location_records,
    molecule_key_from_selected_inp,
    normalize_molecule_key,
    record_from_artifacts,
    reindex_job_locations,
    resolve_job_metadata,
    resolve_record_job_dir,
    resource_dict,
    upsert_job_record,
)
from ._utils import INDEX_DIR_NAME, QUEUE_FILE_NAME

__all__ = [
    "AppConfig",
    "ORCA_AUTO_ORCA_APP_NAME",
    "INDEX_DIR_NAME",
    "QUEUE_FILE_NAME",
    "REPORT_JSON_NAME",
    "REPORT_MD_NAME",
    "STATE_FILE_NAME",
    "JobArtifactContext",
    "JobLocationRecord",
    "JobRuntimeContext",
    "build_job_location_record",
    "collect_reindex_payload",
    "detect_job_type",
    "first_existing_named_file",
    "get_job_location",
    "index_root_for_cfg",
    "is_terminal_status",
    "iter_existing_dirs",
    "job_type_identifier",
    "list_job_location_records",
    "list_job_locations",
    "load_job_artifact_context",
    "load_job_artifacts",
    "load_job_runtime_context",
    "load_orca_contract_payload",
    "load_report_json",
    "load_state",
    "molecule_key_from_selected_inp",
    "normalize_molecule_key",
    "record_from_artifacts",
    "recent_file_candidates",
    "reindex_job_locations",
    "resolve_molecule_key",
    "resolved_path_text",
    "resolve_job_location",
    "resolve_job_metadata",
    "resolve_latest_job_dir",
    "resolve_record_job_dir",
    "resource_dict",
    "safe_is_subpath",
    "upsert_job_location",
    "upsert_job_record",
]
