from __future__ import annotations

from typing import Any

from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _apply_contract_status,
    _apply_submission_result,
    _coerce_bool,
    _engine_stage_sync_context,
    _load_contract_or_none,
    _orchestration_context,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView


def _orca_submission_resource_kwargs(o: Any, enqueue_payload: dict[str, Any]) -> dict[str, Any]:
    resource_kwargs: dict[str, Any] = {}
    max_cores = o.stages.support._safe_int(enqueue_payload.get("max_cores"), default=0)
    max_memory_gb = o.stages.support._safe_int(enqueue_payload.get("max_memory_gb"), default=0)
    if max_cores > 0:
        resource_kwargs["max_cores"] = max_cores
    if max_memory_gb > 0:
        resource_kwargs["max_memory_gb"] = max_memory_gb
    if _coerce_bool(enqueue_payload.get("force", False)):
        resource_kwargs["force"] = True
    return resource_kwargs


def _submit_orca_stage(
    o: Any,
    stage: dict[str, Any],
    task: dict[str, Any],
    task_view: WorkflowTaskView,
    enqueue_payload: dict[str, Any],
    stage_metadata: dict[str, Any],
    *,
    orca_config: str | None,
    orca_repo_root: str | None,
) -> None:
    submission = o.engines.submit_reaction_dir(
        reaction_dir=str(enqueue_payload.get("reaction_dir", "")),
        priority=int(enqueue_payload.get("priority", 10) or 10),
        config_path=str(orca_config),
        repo_root=orca_repo_root,
        **_orca_submission_resource_kwargs(o, enqueue_payload),
    )
    submission["submitted_at"] = o.persistence.now_utc_iso()
    task_view.set_submission_result(submission)
    _apply_submission_result(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        submission=submission,
        metadata_fields=(
            ("queue_id", "queue_id"),
            ("submission_status", "status"),
            ("submitted_at", "submitted_at"),
        ),
    )


def _load_orca_contract(
    o: Any,
    stage_metadata: dict[str, Any],
    *,
    reaction_dir_hint: str,
    orca_config: str | None,
) -> Any | None:
    allowed_root = o.stages.support._load_config_root(orca_config, engine="orca")
    target = (
        o.stages.support._normalize_text(stage_metadata.get("run_id"))
        or reaction_dir_hint
        or o.stages.support._normalize_text(stage_metadata.get("queue_id"))
    )
    if not target:
        return None
    return o.engines.load_orca_artifact_contract(
        target=target,
        orca_allowed_root=allowed_root,
        queue_id=o.stages.support._normalize_text(stage_metadata.get("queue_id")),
        run_id=o.stages.support._normalize_text(stage_metadata.get("run_id")),
        reaction_dir=reaction_dir_hint,
    )


def _apply_orca_contract(
    o: Any,
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    contract: Any,
) -> None:
    _apply_contract_status(stage, task, contract.status)
    task_view.update_orca_contract_payload(contract, o.stages.support._normalize_text)
    stage_view.update_orca_contract_metadata(contract, o.stages.support._normalize_text)
    stage_view.update_orca_attempt_metadata(contract, task_view, o.stages.support._normalize_text)


def _orca_output_artifacts(o: Any, contract: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for spec in _orca_output_artifact_specs(contract):
        _append_orca_output_artifact(o, artifacts, spec)
    return artifacts


def _orca_output_artifact_specs(contract: Any) -> tuple[dict[str, Any], ...]:
    return (
        {
            "kind": "orca_selected_inp",
            "path": contract.selected_inp,
            "selected": True,
            "metadata": {"run_id": contract.run_id},
        },
        {
            "kind": "orca_selected_input_xyz",
            "path": contract.selected_input_xyz,
            "metadata": {"run_id": contract.run_id},
        },
        {
            "kind": "orca_optimized_xyz",
            "path": contract.optimized_xyz_path,
            "selected": contract.status == "completed",
            "metadata": {"run_id": contract.run_id},
        },
        {
            "kind": "orca_last_out",
            "path": contract.last_out_path,
            "selected": contract.status == "completed",
            "metadata": {"analyzer_status": contract.analyzer_status},
        },
        {
            "kind": "orca_run_state",
            "path": contract.run_state_path,
            "metadata": {"status": contract.status},
        },
        {
            "kind": "orca_report_json",
            "path": contract.report_json_path,
            "metadata": {"status": contract.status},
        },
        {
            "kind": "orca_report_md",
            "path": contract.report_md_path,
            "metadata": {"status": contract.status},
        },
        {
            "kind": "orca_output_dir",
            "path": contract.latest_known_path,
            "selected": contract.status in {"completed", "failed", "cancelled"},
            "metadata": {},
        },
    )


def _append_orca_output_artifact(
    o: Any, artifacts: list[dict[str, Any]], spec: dict[str, Any]
) -> None:
    o.stages.runtime._append_unique_artifact(
        artifacts,
        kind=spec["kind"],
        path=spec["path"],
        selected=bool(spec.get("selected", False)),
        metadata=spec.get("metadata"),
    )


def sync_orca_stage_impl(
    stage: dict[str, Any],
    *,
    orca_config: str | None,
    orca_repo_root: str | None,
    submit_ready: bool,
    deps: OrchestrationDeps | None = None,
) -> None:
    context = _engine_stage_sync_context(stage, engine="orca", deps=deps)
    if context is None:
        return
    o = context.o
    task = context.task
    enqueue_payload = task.get("enqueue_payload")
    if not isinstance(enqueue_payload, dict):
        return
    reaction_dir_hint = o.stages.support._normalize_text(
        context.task_payload.get("reaction_dir") or enqueue_payload.get("reaction_dir")
    )
    if context.should_submit(submit_ready=submit_ready, config_path=orca_config):
        _submit_orca_stage(
            o,
            stage,
            task,
            context.task_view,
            enqueue_payload,
            context.stage_metadata,
            orca_config=orca_config,
            orca_repo_root=orca_repo_root,
        )
    contract = _load_orca_contract(
        o,
        context.stage_metadata,
        reaction_dir_hint=reaction_dir_hint,
        orca_config=orca_config,
    )
    if contract is None:
        return
    _apply_orca_contract(
        o,
        stage,
        task,
        context.stage_view,
        context.task_view,
        contract,
    )
    context.set_output_artifacts(_orca_output_artifacts(o, contract))


def completed_orca_stage_impl(
    stage: dict[str, Any],
    *,
    orca_config: str | None,
    deps: OrchestrationDeps | None = None,
) -> Any | None:
    o = _orchestration_context(deps)
    task = stage.get("task")
    if not isinstance(task, dict):
        return None
    payload = o.stages.support._task_payload_dict(task)
    enqueue_payload = o.stages.support._coerce_mapping(task.get("enqueue_payload"))
    stage_metadata = o.stages.support._stage_metadata(stage)
    reaction_dir_hint = o.stages.support._normalize_text(
        payload.get("reaction_dir") or enqueue_payload.get("reaction_dir")
    )
    target = (
        o.stages.support._normalize_text(stage_metadata.get("run_id"))
        or reaction_dir_hint
        or o.stages.support._normalize_text(stage_metadata.get("queue_id"))
    )
    if not target:
        return None
    return _load_contract_or_none(
        o.engines.load_orca_artifact_contract,
        engine="orca",
        target=target,
        stage=stage,
        orca_allowed_root=o.stages.support._load_config_root(orca_config, engine="orca"),
        queue_id=o.stages.support._normalize_text(stage_metadata.get("queue_id")),
        run_id=o.stages.support._normalize_text(stage_metadata.get("run_id")),
        reaction_dir=reaction_dir_hint,
    )
