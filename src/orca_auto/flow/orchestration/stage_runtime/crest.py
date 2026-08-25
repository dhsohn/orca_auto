from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.engine_process import atomic_write_confined_bytes, ensure_confined_directory
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.utils import normalize_text, safe_int
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _apply_contract_status,
    _apply_submission_result,
    _engine_job_dir_contract_lookup,
    _engine_stage_sync_context,
    _load_contract_or_none,
    _manifest_override_mapping,
    _workflow_internal_runs_root,
)
from orca_auto.flow.orchestration.stage_views import (
    WorkflowPayloadView,
    WorkflowStageView,
    WorkflowTaskView,
)
from orca_auto.flow.orchestration.support import load_config_root_impl, submission_target_impl
from orca_auto.flow.state import workflow_workspace_internal_engine_paths


def ensure_crest_job_dir_impl(
    stage: dict[str, Any],
    *,
    crest_allowed_root: Path,
    workflow_id: str,
) -> str:
    del workflow_id
    stage_view = WorkflowStageView(stage)
    task_view = stage_view.task
    payload = task_view.payload()
    existing = normalize_text(payload.get("job_dir"))
    if existing:
        return existing
    stage_id = stage_view.stage_id()
    job_dir = crest_allowed_root / stage_id
    ensure_confined_directory(crest_allowed_root, job_dir, label="CREST stage job directory")
    input_target = job_dir / "input.xyz"
    atomic_write_confined_bytes(
        job_dir,
        input_target,
        read_stable_regular_file(Path(payload["source_input_xyz"]).expanduser()),
        label="CREST materialized input",
    )
    overrides = _manifest_override_mapping(payload.get("job_manifest_overrides"))
    task_resource_request = task_view.resource_request()
    manifest_payload: dict[str, Any] = {
        "mode": normalize_text(payload.get("mode")) or "standard",
        "speed": "quick",
        "gfn": 2,
    }
    for key, value in overrides.items():
        if key == "input_xyz":
            continue
        manifest_payload[key] = value
    manifest_payload["resources"] = {
        "max_cores": safe_int(task_resource_request.get("max_cores"), default=8),
        "max_memory_gb": safe_int(task_resource_request.get("max_memory_gb"), default=32),
    }
    manifest_payload["input_xyz"] = "input.xyz"
    atomic_write_confined_bytes(
        job_dir,
        job_dir / "crest_job.yaml",
        yaml.safe_dump(manifest_payload, sort_keys=False, allow_unicode=False).encode("utf-8"),
        label="CREST materialized manifest",
    )
    task_view.record_crest_job_materialization(job_dir=job_dir, input_target=input_target)
    return str(job_dir)


def _submit_crest_stage(
    services: OrchestrationServices,
    stage: dict[str, Any],
    task: dict[str, Any],
    *,
    crest_runtime_paths: dict[str, Path],
    crest_config: str | None,
    workflow_id: str,
) -> bool:
    task_view = WorkflowTaskView(task)
    job_dir = ensure_crest_job_dir_impl(
        stage,
        crest_allowed_root=crest_runtime_paths["allowed_root"],
        workflow_id=workflow_id,
    )
    enqueue_payload = task_view.enqueue_payload()
    submission = services.engines.submit_crest_job_dir(
        job_dir=job_dir,
        priority=normalize_queue_priority(enqueue_payload.get("priority")),
        config_path=str(crest_config),
    )
    submission["submitted_at"] = services.clock.now_utc_iso()
    task_view.set_submission_result(submission)
    stage_metadata = WorkflowStageView(stage).metadata()
    return _apply_submission_result(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        submission=submission,
        metadata_fields=(("queue_id", "queue_id"), ("child_job_id", "job_id")),
    )


def _load_crest_contract(
    services: OrchestrationServices,
    stage: dict[str, Any],
    task: dict[str, Any],
    *,
    crest_runtime_paths: dict[str, Path],
    crest_config: str | None,
) -> Any | None:
    payload = WorkflowTaskView(task).payload()
    lookup = _engine_job_dir_contract_lookup(
        services,
        stage,
        payload,
        runtime_paths=crest_runtime_paths,
        config_path=crest_config,
        engine="crest",
    )
    if lookup is None:
        return None
    target, index_root = lookup
    return _load_contract_or_none(
        services.engines.load_crest_artifact_contract,
        engine="crest",
        target=target,
        stage=stage,
        crest_index_root=index_root,
    )


def _apply_crest_contract(
    stage: dict[str, Any],
    task: dict[str, Any],
    contract: Any,
) -> None:
    _apply_contract_status(stage, task, contract.status)
    stage_view = WorkflowStageView(stage)
    stage_view.update_crest_contract_metadata(contract)
    WorkflowTaskView(task).update_crest_contract_payload(contract)
    stage_view.set_crest_conformer_artifacts(contract)


def sync_crest_stage_impl(
    stage: dict[str, Any],
    *,
    crest_config: str | None,
    submit_ready: bool,
    workflow_id: str,
    workspace_dir: Path,
    services: OrchestrationServices | None = None,
) -> None:
    context = _engine_stage_sync_context(stage, engine="crest", services=services)
    if context is None:
        return
    resolved = context.services
    task = context.task
    crest_runtime_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="crest")
    if context.should_submit(submit_ready=submit_ready, config_path=crest_config):
        submission_applied = _submit_crest_stage(
            resolved,
            stage,
            task,
            crest_runtime_paths=crest_runtime_paths,
            crest_config=crest_config,
            workflow_id=workflow_id,
        )
        if not submission_applied:
            # The job directory still belongs to an active cancellation.  Its
            # old contract must not overwrite PLANNED in this same tick; the
            # next sync retries submission and publishes a new identity once
            # cancellation becomes terminal.
            return
    contract = _load_crest_contract(
        resolved,
        stage,
        task,
        crest_runtime_paths=crest_runtime_paths,
        crest_config=crest_config,
    )
    if contract is not None:
        _apply_crest_contract(stage, task, contract)


def completed_crest_roles_impl(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    latest_by_role: dict[str, dict[str, Any]] = {}
    for stage_view in WorkflowPayloadView(payload).stage_views:
        task_view = stage_view.existing_task
        if task_view is None or task_view.engine() != "crest":
            continue
        task_payload = task_view.existing_payload()
        stage_metadata = stage_view.existing_metadata() or {}
        role = normalize_text(stage_metadata.get("input_role")).lower()
        if not role and isinstance(task_payload, dict):
            role = normalize_text(task_payload.get("input_role")).lower()
        if role:
            latest_by_role[role] = stage_view.raw
    rows: dict[str, dict[str, Any]] = {}
    for role, stage in latest_by_role.items():
        status = WorkflowStageView(stage).status_pair()
        if status.stage == "completed" and status.task in {"", "completed"}:
            rows[role] = stage
    return rows


def completed_crest_stage_impl(
    stage: dict[str, Any],
    *,
    crest_config: str | None,
    services: OrchestrationServices | None = None,
) -> Any | None:
    resolved = resolve_orchestration_services(services)
    task_view = WorkflowStageView(stage).existing_task
    if task_view is None:
        return None
    payload = task_view.payload()
    job_dir_target = normalize_text(payload.get("job_dir"))
    index_root = (
        _workflow_internal_runs_root(job_dir_target, engine="crest")
        or load_config_root_impl(crest_config, engine="crest", services=resolved)
        or (
            Path(job_dir_target).expanduser().resolve().parent
            if job_dir_target
            else Path(".").resolve().parent
        )
    )
    target = job_dir_target or submission_target_impl(stage)
    if not target:
        return None
    return _load_contract_or_none(
        resolved.engines.load_crest_artifact_contract,
        engine="crest",
        target=target,
        stage=stage,
        crest_index_root=index_root,
    )
