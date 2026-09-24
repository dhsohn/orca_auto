"""ORCA rows of the shared job-location index (``job_locations.json``).

``_records`` builds and upserts one row, ``_artifacts_to_records`` projects
a directory's state and report artifacts onto a row, ``_rebuild`` re-derives
the index from every state on disk, ``_generation`` matches payloads to a
queue generation and ``_utils`` holds the text and resource normalizers.
Consumers import only the names exported here.
"""

from __future__ import annotations

from ._artifacts_to_records import record_from_artifacts
from ._rebuild import (
    JobLocationRebuildConflict,
    JobLocationRebuildResult,
    rebuild_job_location_records,
)
from ._records import (
    index_root_for_cfg,
    list_job_location_records,
    resolve_job_metadata,
    resolve_record_job_dir,
    resource_dict,
    upsert_job_record,
)

__all__ = [
    "JobLocationRebuildConflict",
    "JobLocationRebuildResult",
    "index_root_for_cfg",
    "list_job_location_records",
    "rebuild_job_location_records",
    "record_from_artifacts",
    "resolve_job_metadata",
    "resolve_record_job_dir",
    "resource_dict",
    "upsert_job_record",
]
