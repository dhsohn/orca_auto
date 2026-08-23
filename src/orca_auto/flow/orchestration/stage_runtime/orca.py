from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.statuses import STATUS_FAILED
from orca_auto.core.utils import mapping_or_empty, normalize_text, safe_int
from orca_auto.flow._orca_stage_materialization import validate_workflow_orca_input
from orca_auto.flow.contracts.workflow import workflow_stage_metadata, workflow_task_payload_dict
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _apply_contract_status,
    _apply_submission_result,
    _coerce_bool,
    _engine_stage_sync_context,
    _load_contract_or_none,
    append_unique_artifact_impl,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView
from orca_auto.flow.orchestration.support import load_config_root_impl
from orca_auto.orca.execution import select_latest_inp


@dataclass(frozen=True)
class _OrcaSubmissionBinding:
    reaction_dir: str
    selected_inp: Path


def _orca_submission_resource_kwargs(enqueue_payload: dict[str, Any]) -> dict[str, Any]:
    resource_kwargs: dict[str, Any] = {}
    max_cores = safe_int(enqueue_payload.get("max_cores"), default=0)
    max_memory_gb = safe_int(enqueue_payload.get("max_memory_gb"), default=0)
    if max_cores > 0:
        resource_kwargs["max_cores"] = max_cores
    if max_memory_gb > 0:
        resource_kwargs["max_memory_gb"] = max_memory_gb
    if _coerce_bool(enqueue_payload.get("force", False)):
        resource_kwargs["force"] = True
    return resource_kwargs


def _submit_orca_stage(
    services: OrchestrationServices,
    stage: dict[str, Any],
    task: dict[str, Any],
    task_view: WorkflowTaskView,
    enqueue_payload: dict[str, Any],
    stage_metadata: dict[str, Any],
    *,
    reaction_dir: str,
    submission_binding: _OrcaSubmissionBinding,
    orca_config: str | None,
    orca_repo_root: str | None,
) -> None:
    submission = services.engines.submit_reaction_dir(
        reaction_dir=reaction_dir,
        priority=normalize_queue_priority(enqueue_payload.get("priority")),
        config_path=str(orca_config),
        repo_root=orca_repo_root,
        expected_selected_inp=str(submission_binding.selected_inp),
        workflow_task_kind=task_view.kind(),
        **_orca_submission_resource_kwargs(enqueue_payload),
    )
    submission["submitted_at"] = services.clock.now_utc_iso()
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
    services: OrchestrationServices,
    stage_metadata: dict[str, Any],
    *,
    reaction_dir_hint: str,
    orca_config: str | None,
) -> Any | None:
    allowed_root = load_config_root_impl(orca_config, engine="orca", services=services)
    target = (
        normalize_text(stage_metadata.get("run_id"))
        or reaction_dir_hint
        or normalize_text(stage_metadata.get("queue_id"))
    )
    if not target:
        return None
    return services.engines.load_orca_artifact_contract(
        target=target,
        orca_allowed_root=allowed_root,
        queue_id=normalize_text(stage_metadata.get("queue_id")),
        run_id=normalize_text(stage_metadata.get("run_id")),
        reaction_dir=reaction_dir_hint,
    )


def _apply_orca_contract(
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    contract: Any,
) -> None:
    _apply_contract_status(stage, task, contract.status)
    task_view.update_orca_contract_payload(contract, normalize_text)
    stage_view.update_orca_contract_metadata(contract, normalize_text)
    stage_view.update_orca_attempt_metadata(contract, task_view, normalize_text)


def _validated_orca_contract(task_view: WorkflowTaskView, contract: Any) -> Any:
    if normalize_text(getattr(contract, "status", "")).lower() != "completed":
        return contract
    selected_inp = normalize_text(getattr(contract, "selected_inp", ""))
    try:
        validate_workflow_orca_input(
            task_kind=task_view.kind(),
            inp_path=Path(selected_inp) if selected_inp else None,
        )
    except ValueError as exc:
        return replace(contract, status="failed", reason=str(exc))
    return contract


def _strict_durable_path_pair(
    context: Any,
    enqueue_payload: dict[str, Any],
    *,
    field: str,
    expect_directory: bool,
) -> Path:
    task_value = context.task_payload.get(field)
    enqueue_value = enqueue_payload.get(field)
    task_text = task_value.strip() if isinstance(task_value, str) else ""
    enqueue_text = enqueue_value.strip() if isinstance(enqueue_value, str) else ""
    if not task_text or not enqueue_text:
        raise ValueError(
            "workflow ORCA route-role mismatch: role stage requires durable "
            f"{field} in both task payload and enqueue payload"
        )
    try:
        task_path = Path(task_text).expanduser().resolve(strict=True)
        enqueue_path = Path(enqueue_text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            f"workflow ORCA route-role mismatch: durable {field} path cannot be resolved safely"
        ) from exc
    if task_path != enqueue_path:
        raise ValueError(
            "workflow ORCA route-role mismatch: durable "
            f"{field} values differ between task payload and enqueue payload"
        )
    valid_kind = task_path.is_dir() if expect_directory else task_path.is_file()
    if not valid_kind:
        expected = "directory" if expect_directory else "regular file"
        raise ValueError(
            f"workflow ORCA route-role mismatch: durable {field} must resolve to a {expected}"
        )
    return task_path


def _validate_orca_input_before_submission(
    context: Any,
    enqueue_payload: dict[str, Any],
) -> _OrcaSubmissionBinding:
    """Validate the input the direct submitter will actually select."""

    reaction_dir = _strict_durable_path_pair(
        context,
        enqueue_payload,
        field="reaction_dir",
        expect_directory=True,
    )
    expected_inp = _strict_durable_path_pair(
        context,
        enqueue_payload,
        field="selected_inp",
        expect_directory=False,
    )
    try:
        selected_inp = select_latest_inp(reaction_dir)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "workflow ORCA route-role mismatch: "
            f"task_kind={context.task_view.kind()!r} requires the actual submission "
            f"input to be selected safely from reaction_dir={str(reaction_dir)!r}: {exc}"
        ) from exc
    if expected_inp != selected_inp:
        raise ValueError(
            "workflow ORCA route-role mismatch: actual submission input differs from "
            "durable selected_inp; "
            f"actual={str(selected_inp)!r}, expected={str(expected_inp)!r}"
        )
    validate_workflow_orca_input(task_kind=context.task_view.kind(), inp_path=selected_inp)
    return _OrcaSubmissionBinding(
        reaction_dir=str(reaction_dir),
        selected_inp=selected_inp,
    )


def _orca_output_artifacts(contract: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for spec in _orca_output_artifact_specs(contract):
        _append_orca_output_artifact(artifacts, spec)
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
            "kind": "orca_output_dir",
            "path": contract.latest_known_path,
            "selected": contract.status in {"completed", "failed", "cancelled"},
            "metadata": {},
        },
    )


def _append_orca_output_artifact(artifacts: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    append_unique_artifact_impl(
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
    services: OrchestrationServices | None = None,
) -> None:
    context = _engine_stage_sync_context(stage, engine="orca", services=services)
    if context is None:
        return
    resolved = context.services
    task = context.task
    enqueue_payload = task.get("enqueue_payload")
    if not isinstance(enqueue_payload, dict):
        return
    reaction_dir_hint = normalize_text(
        context.task_payload.get("reaction_dir") or enqueue_payload.get("reaction_dir")
    )
    if context.should_submit(submit_ready=submit_ready, config_path=orca_config):
        try:
            submission_binding = _validate_orca_input_before_submission(context, enqueue_payload)
        except ValueError as exc:
            context.stage_view.set_status_pair(
                stage_status=STATUS_FAILED,
                task_status=STATUS_FAILED,
            )
            context.stage_metadata["reason"] = str(exc)
            return
        reaction_dir_hint = submission_binding.reaction_dir
        _submit_orca_stage(
            resolved,
            stage,
            task,
            context.task_view,
            enqueue_payload,
            context.stage_metadata,
            reaction_dir=reaction_dir_hint,
            submission_binding=submission_binding,
            orca_config=orca_config,
            orca_repo_root=orca_repo_root,
        )
    contract = _load_orca_contract(
        resolved,
        context.stage_metadata,
        reaction_dir_hint=reaction_dir_hint,
        orca_config=orca_config,
    )
    if contract is None:
        return
    contract = _validated_orca_contract(context.task_view, contract)
    _apply_orca_contract(
        stage,
        task,
        context.stage_view,
        context.task_view,
        contract,
    )
    context.set_output_artifacts(_orca_output_artifacts(contract))


def completed_orca_stage_impl(
    stage: dict[str, Any],
    *,
    orca_config: str | None,
    services: OrchestrationServices | None = None,
) -> Any | None:
    resolved = resolve_orchestration_services(services)
    task = stage.get("task")
    if not isinstance(task, dict):
        return None
    payload = workflow_task_payload_dict(task)
    enqueue_payload = mapping_or_empty(task.get("enqueue_payload"))
    stage_metadata = workflow_stage_metadata(stage)
    reaction_dir_hint = normalize_text(
        payload.get("reaction_dir") or enqueue_payload.get("reaction_dir")
    )
    target = (
        normalize_text(stage_metadata.get("run_id"))
        or reaction_dir_hint
        or normalize_text(stage_metadata.get("queue_id"))
    )
    if not target:
        return None
    return _load_contract_or_none(
        resolved.engines.load_orca_artifact_contract,
        engine="orca",
        target=target,
        stage=stage,
        orca_allowed_root=load_config_root_impl(orca_config, engine="orca", services=resolved),
        queue_id=normalize_text(stage_metadata.get("queue_id")),
        run_id=normalize_text(stage_metadata.get("run_id")),
        reaction_dir=reaction_dir_hint,
    )
