from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from ..config import AppConfig
from ..job_locations import resource_dict, upsert_job_record
from ..organize_index import to_reaction_relative_path
from ..result_organizer_models import OrganizePlan
from ..state import ORGANIZED_REF_NAME, now_utc_iso, write_organized_ref
from ..types import RunState


def build_index_record(plan: OrganizePlan, state: Mapping[str, Any]) -> dict[str, Any]:
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        final_result = {}

    attempts = state.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0

    return {
        "run_id": plan.run_id,
        "reaction_dir": str(plan.target_abs_path),
        "status": state.get("status", ""),
        "analyzer_status": final_result.get("analyzer_status", ""),
        "reason": final_result.get("reason", ""),
        "job_type": plan.job_type,
        "molecule_key": plan.molecule_key,
        "selected_inp": to_reaction_relative_path(
            state.get("selected_inp", ""), plan.target_abs_path
        ),
        "last_out_path": to_reaction_relative_path(
            final_result.get("last_out_path", ""), plan.target_abs_path
        ),
        "attempt_count": attempt_count,
        "completed_at": final_result.get("completed_at", ""),
        "organized_at": now_utc_iso(),
        "organized_path": plan.target_rel_path,
    }


def tracking_resources(cfg: AppConfig) -> dict[str, int]:
    return resource_dict(
        cfg.resources.max_cores_per_task,
        cfg.resources.max_memory_gb_per_task,
    )


def tracking_job_id(plan: OrganizePlan, state: RunState) -> str:
    return str(state.get("job_id") or state.get("run_id") or plan.run_id).strip()


def write_tracking_after_move(
    cfg: AppConfig,
    *,
    plan: OrganizePlan,
    state_after_move: RunState,
) -> None:
    job_id = tracking_job_id(plan, state_after_move)
    requested = tracking_resources(cfg)
    selected_inp = str(state_after_move.get("selected_inp") or "").strip()

    plan.source_dir.mkdir(parents=True, exist_ok=True)
    write_organized_ref(
        plan.source_dir,
        {
            "job_id": job_id,
            "run_id": plan.run_id,
            "original_run_dir": str(plan.source_dir),
            "organized_output_dir": str(plan.target_abs_path),
            "organized_at": now_utc_iso(),
            "status": str(state_after_move.get("status") or "completed"),
            "job_type": plan.job_type,
            "selected_inp": selected_inp,
            "selected_input_xyz": selected_inp,
            "molecule_key": plan.molecule_key,
            "resource_request": requested,
            "resource_actual": requested,
        },
    )
    upsert_job_record(
        cfg,
        job_id=job_id,
        status=str(state_after_move.get("status") or "completed"),
        job_dir=plan.source_dir,
        job_type=plan.job_type,
        selected_input_xyz=selected_inp,
        organized_output_dir=plan.target_abs_path,
        molecule_key=plan.molecule_key,
        resource_request=requested,
        resource_actual=requested,
    )


def cleanup_organized_ref_stub(plan: OrganizePlan) -> None:
    organized_ref_path = plan.source_dir / ORGANIZED_REF_NAME
    if organized_ref_path.exists():
        organized_ref_path.unlink()
    with suppress(OSError):
        plan.source_dir.rmdir()


def restore_tracking_after_rollback(
    cfg: AppConfig,
    *,
    plan: OrganizePlan,
    state_after_rollback: RunState,
) -> None:
    job_id = tracking_job_id(plan, state_after_rollback)
    requested = tracking_resources(cfg)
    upsert_job_record(
        cfg,
        job_id=job_id,
        status=str(state_after_rollback.get("status") or "completed"),
        job_dir=plan.source_dir,
        job_type=plan.job_type,
        selected_input_xyz=str(state_after_rollback.get("selected_inp") or "").strip(),
        molecule_key=plan.molecule_key,
        resource_request=requested,
        resource_actual=requested,
    )
