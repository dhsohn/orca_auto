from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import mapping_or_empty, safe_int
from orca_auto.flow.contracts.workflow import workflow_task_payload_dict
from orca_auto.flow.orchestration.stage_views import WorkflowStageView


def xtb_attempt_record_impl(
    stage: dict[str, Any],
    *,
    attempt_number: int,
) -> dict[str, Any]:
    return WorkflowStageView(stage).xtb_attempt_record(attempt_number)


def xtb_retry_recipe_impl(attempt_number: int) -> dict[str, Any]:
    attempt = max(0, int(attempt_number))
    if attempt <= 0:
        return {
            "attempt_number": 0,
            "recipe_id": "baseline",
            "recipe_label": "baseline",
            "xcontrol_name": "",
            "xcontrol_lines": (),
        }
    if attempt == 1:
        return {
            "attempt_number": 1,
            "recipe_id": "path_input_recommended",
            "recipe_label": "recommended_path_input",
            "xcontrol_name": "path_retry_01.inp",
            "xcontrol_lines": (
                "$path",
                "   nrun=1",
                "   npoint=25",
                "   anopt=10",
                "   kpush=0.003",
                "   kpull=-0.015",
                "   ppull=0.05",
                "   alp=1.2",
                "$end",
            ),
        }
    return {
        "attempt_number": attempt,
        "recipe_id": "path_input_refined",
        "recipe_label": "refined_path_input",
        "xcontrol_name": f"path_retry_{attempt:02d}.inp",
        "xcontrol_lines": (
            "$path",
            "   nrun=2",
            "   npoint=35",
            "   anopt=15",
            "   kpush=0.003",
            "   kpull=-0.015",
            "   ppull=0.05",
            "   alp=1.2",
            "$end",
        ),
    }


def xtb_path_retry_limit_impl(stage: dict[str, Any]) -> int:
    task = stage.get("task")
    if not isinstance(task, dict):
        return 2
    payload = workflow_task_payload_dict(task)
    metadata = mapping_or_empty(task.get("metadata"))
    return max(
        0,
        safe_int(
            payload.get("max_handoff_retries", metadata.get("max_handoff_retries", 2)),
            default=2,
        ),
    )


def xtb_current_attempt_number_impl(stage: dict[str, Any]) -> int:
    return WorkflowStageView(stage).xtb_current_attempt_number()


def _xtb_path_job_dir(xtb_allowed_root: Path, stage_id: str, attempt_number: int) -> Path:
    base_dir = xtb_allowed_root / stage_id
    if attempt_number == 0:
        return base_dir
    return base_dir / f"retry_attempt_{attempt_number:02d}"


__all__ = [
    "xtb_attempt_record_impl",
    "xtb_current_attempt_number_impl",
    "xtb_path_retry_limit_impl",
    "xtb_retry_recipe_impl",
]
