from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

_WorkflowStageT = TypeVar("_WorkflowStageT")


def candidate_source_payload(candidate: Any) -> dict[str, Any]:
    return {
        "source_job_id": candidate.source_job_id,
        "source_job_type": candidate.source_job_type,
        "reaction_key": candidate.reaction_key,
        "rank": candidate.rank,
        "kind": candidate.kind,
    }


def task_payload(
    *,
    ctx: Any,
    materialized: Any,
    resource_request: dict[str, int],
    cli_command: str,
) -> dict[str, Any]:
    return {
        "stage_id": ctx.stage_id,
        "engine": "orca",
        "task_kind": ctx.task_kind,
        "selected_input_xyz": materialized.selected_xyz,
        "selected_input_label": ctx.selected_input_label,
        "source_job_id": ctx.candidate.source_job_id,
        "source_job_type": ctx.candidate.source_job_type,
        "reaction_key": ctx.candidate.reaction_key,
        "workflow_id": ctx.workflow_id,
        "template_name": ctx.template_name,
        "resource_request": dict(resource_request),
        "reaction_dir": materialized.reaction_dir,
        "selected_inp": materialized.selected_inp,
        "suggested_command": f"{cli_command} run-dir '{materialized.reaction_dir}'",
        "metadata": {
            "candidate_rank": ctx.candidate.rank,
            "candidate_kind": ctx.candidate.kind,
            "candidate_score": ctx.candidate.score,
            "candidate_selected": ctx.candidate.selected,
            "candidate_metadata": dict(ctx.candidate.metadata),
            "source_selected_input_xyz": ctx.candidate.selected_input_xyz,
        },
    }


def workflow_task(
    *,
    ctx: Any,
    materialized: Any,
    resource_request: dict[str, int],
    enqueue_payload: dict[str, Any],
    task_payload: dict[str, Any],
    workflow_task_cls: Any,
) -> Any:
    return workflow_task_cls.from_raw(
        task_id=f"{ctx.workflow_id}:{ctx.stage_id}",
        engine="orca",
        task_kind=ctx.task_kind,
        resource_request=resource_request,
        payload=task_payload,
        enqueue_payload=enqueue_payload,
        metadata={
            "workflow_id": ctx.workflow_id,
            "template_name": ctx.template_name,
            "source_candidate_path": ctx.candidate.artifact_path,
            "queue_priority": int(ctx.priority),
            "reaction_dir": materialized.reaction_dir,
            "selected_inp": materialized.selected_inp,
        },
    )


def input_artifact_metadata(candidate: Any) -> dict[str, Any]:
    return {
        "rank": candidate.rank,
        "kind": candidate.kind,
        "score": candidate.score,
        **dict(candidate.metadata),
    }


def workflow_stage(
    *,
    ctx: Any,
    materialized: Any,
    task: Any,
    workflow_stage_cls: Callable[..., _WorkflowStageT],
    artifact_ref_cls: Any,
) -> _WorkflowStageT:
    return workflow_stage_cls(
        stage_id=ctx.stage_id,
        stage_kind="orca_stage",
        status="planned",
        input_artifacts=(
            artifact_ref_cls(
                kind=ctx.input_artifact_kind,
                path=ctx.candidate.artifact_path,
                selected=ctx.candidate.selected,
                metadata=input_artifact_metadata(ctx.candidate),
            ),
        ),
        output_artifacts=(
            artifact_ref_cls(
                kind="orca_input",
                path=materialized.selected_inp,
                selected=True,
                metadata={
                    "engine": "orca",
                    "task_kind": ctx.task_kind,
                    "reaction_dir": materialized.reaction_dir,
                },
            ),
        ),
        task=task,
        metadata={
            "candidate_rank": ctx.candidate.rank,
            "candidate_kind": ctx.candidate.kind,
            "candidate_score": ctx.candidate.score,
            "selected_input_label": ctx.selected_input_label,
            "reaction_dir": materialized.reaction_dir,
        },
    )
