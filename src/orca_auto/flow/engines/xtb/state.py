from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    JOB_STATE_FILE,
    XTB_JOB_MANIFEST_FILE,
)
from orca_auto.core.state import engine as _engine_state
from orca_auto.core.utils import now_utc_iso

STATE_FILE_NAME = JOB_STATE_FILE
RECOVERY_PENDING_REASONS = _engine_state.RECOVERY_PENDING_REASONS
_STATE_ACCESS = _engine_state.EngineStateOnlyAccess(state_file_name=STATE_FILE_NAME)
_RECOVERY_PENDING = _engine_state.EngineRecoveryPendingWriter(
    access=_STATE_ACCESS,
    manifest_filename=XTB_JOB_MANIFEST_FILE,
    engine="xtb",
    now_fn=lambda: now_utc_iso(),
)
_RECOVERY_RETAINED_FIELDS = _engine_state.RecoveryRetainedFieldsSpec(
    int_fields=("candidate_count",),
    list_fields=("candidate_paths", "selected_candidate_paths", "candidate_details"),
    dict_fields=("analysis_summary",),
)
write_state = _STATE_ACCESS.write_state
load_state = _STATE_ACCESS.load_state


def state_matches_job(
    state: dict[str, Any] | None,
    *,
    selected_input_xyz: str | Path,
    job_type: str,
    reaction_key: str,
) -> bool:
    return _engine_state.state_matches_engine_job(
        state,
        selected_input_xyz=selected_input_xyz,
        job_type=job_type,
        reaction_key=reaction_key,
    )


is_recovery_pending = _engine_state.is_recovery_pending_state


def _recovery_retained_fields(
    existing: dict[str, Any],
    input_summary_payload: dict[str, Any],
) -> dict[str, Any]:
    return _engine_state.recovery_retained_fields(
        existing,
        _RECOVERY_RETAINED_FIELDS,
        list_fallbacks={"candidate_paths": input_summary_payload.get("candidate_paths")},
    )


def mark_recovery_pending(
    job_dir: Path,
    *,
    job_id: str,
    selected_input_xyz: str | Path,
    job_type: str,
    reaction_key: str,
    input_summary: dict[str, Any] | None,
    resource_request: dict[str, Any] | None,
    resource_actual: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    input_summary_payload = _engine_state.coerce_dict(input_summary)
    return _engine_state.write_recovery_pending_state(
        _RECOVERY_PENDING,
        job_dir,
        job_id=job_id,
        selected_input_xyz=selected_input_xyz,
        reason=reason,
        identity_fields=_engine_state.recovery_identity_payload(
            {
                "job_type": job_type,
                "reaction_key": reaction_key,
            },
            extra_fields={"input_summary": input_summary_payload},
        ),
        retained_fields=lambda existing: _recovery_retained_fields(existing, input_summary_payload),
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
