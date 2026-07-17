from __future__ import annotations

import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import (
    validate_workflow_workspace_identity,
    workflow_root_for_workspace,
)
from orca_auto.core.statuses import STATUS_CANCELLED
from orca_auto.core.utils import normalize_text as _normalize_text

from ..registry import sync_workflow_registry
from ..state import load_workflow_payload, workflow_summary, write_workflow_payload
from ..workflow.status import WORKFLOW_FAILED_STATUSES
from .settings import _apply_flow_restart_settings, _stage_should_rematerialize
from .stage_ops import (
    _active_restart_error,
    _active_stage_rows,
    _clear_phase_notification_state,
    _reset_stage_for_restart,
    _stage_needs_restart,
)

_RESTARTABLE_WORKFLOW_STATUSES = frozenset({*WORKFLOW_FAILED_STATUSES, STATUS_CANCELLED})
_INTERACTION_ROLE_PREFIX = "interaction_"


@dataclass
class RestartDirectoryTransaction:
    created_dirs: list[Path] = field(default_factory=list)
    payload_committed: bool = False
    preserve_created_dirs: bool = False
    original_payload: dict[str, Any] | None = None

    def capture_original_payload(self, payload: dict[str, Any]) -> None:
        self.original_payload = deepcopy(payload)

    def mark_payload_committed(self) -> None:
        self.payload_committed = True

    def mark_payload_outcome_ambiguous(self) -> None:
        self.preserve_created_dirs = True

    def cleanup_uncommitted(self) -> None:
        if self.payload_committed or self.preserve_created_dirs:
            return
        for path in reversed(self.created_dirs):
            shutil.rmtree(path, ignore_errors=True)
        self.created_dirs.clear()


@dataclass(frozen=True)
class WorkflowRestartMutation:
    root: Path
    workspace: Path
    payload: dict[str, Any]
    previous_status: str
    restarted_at: str
    restarted_stages: list[dict[str, str]]
    flow_manifest_applied: bool
    summary: dict[str, Any]

    @property
    def workflow_id(self) -> str:
        return _normalize_text(self.payload.get("workflow_id"))

    @property
    def template_name(self) -> str:
        return _normalize_text(self.payload.get("template_name"))

    def journal_metadata(self) -> dict[str, Any]:
        return {
            "workspace_dir": str(self.workspace),
            "restarted_count": len(self.restarted_stages),
            "flow_manifest_applied": self.flow_manifest_applied,
            "stages": self.restarted_stages,
        }

    def response_payload(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "template_name": self.template_name,
            "workspace_dir": str(self.workspace),
            "workflow_root": str(self.root),
            "status": "restarted",
            "workflow_status": "planned",
            "previous_status": self.previous_status,
            "restarted_count": len(self.restarted_stages),
            "restarted_stages": self.restarted_stages,
            "summary": self.summary,
        }


def _restart_paths(
    *,
    workspace_dir: str | Path,
    workflow_root: str | Path | None,
) -> tuple[Path, Path]:
    workspace = Path(workspace_dir).expanduser().resolve()
    if workflow_root is not None:
        return workspace, Path(workflow_root).expanduser().resolve()
    return workspace, workflow_root_for_workspace(workspace)


def _validate_restart_request(
    payload: dict[str, Any],
    *,
    workspace: Path,
    force: bool,
) -> tuple[str, str]:
    previous_status = _normalize_text(payload.get("status")).lower()
    workflow_id = validate_workflow_workspace_identity(
        workspace,
        payload.get("workflow_id"),
    )
    if previous_status not in _RESTARTABLE_WORKFLOW_STATUSES and not force:
        raise ValueError(
            f"workflow is not failed or cancelled: {payload.get('workflow_id', workspace.name)} "
            f"(status={previous_status or 'unknown'})"
        )

    active_stages = _active_stage_rows(payload)
    if active_stages:
        raise _active_restart_error(workflow_id, active_stages)
    return previous_status, workflow_id


def _reset_restartable_stages(
    payload: dict[str, Any],
    *,
    flow_settings: dict[str, Any],
    restart_allowed_root: Path,
    directory_transaction: RestartDirectoryTransaction | None = None,
) -> list[dict[str, str]]:
    restarted_stages: list[dict[str, str]] = []
    has_interaction_stages = any(
        isinstance(raw_stage, dict)
        and _normalize_text(
            raw_stage.get("metadata", {}).get("role")
            if isinstance(raw_stage.get("metadata"), dict)
            else ""
        ).startswith(_INTERACTION_ROLE_PREFIX)
        for raw_stage in payload.get("stages", [])
    )
    if has_interaction_stages and not flow_settings.get("interaction_energy_disabled"):
        reopening_primary_orca = []
        for raw_stage in payload.get("stages", []):
            if not isinstance(raw_stage, dict) or not _stage_needs_restart(raw_stage):
                continue
            metadata = raw_stage.get("metadata")
            role = _normalize_text(metadata.get("role")) if isinstance(metadata, dict) else ""
            if _normalize_text(raw_stage.get("stage_kind")) == "orca_stage" and not role.startswith(
                _INTERACTION_ROLE_PREFIX
            ):
                reopening_primary_orca.append(_normalize_text(raw_stage.get("stage_id")))
        if reopening_primary_orca:
            raise ValueError(
                "cannot restart primary ORCA stages after interaction-energy fan-out; "
                "disable interaction_energy to retire its stages first: "
                + ", ".join(reopening_primary_orca)
            )
    if flow_settings.get("interaction_energy_disabled"):
        retained: list[Any] = []
        for raw_stage in payload.get("stages", []):
            if not isinstance(raw_stage, dict):
                retained.append(raw_stage)
                continue
            metadata = raw_stage.get("metadata")
            role = _normalize_text(metadata.get("role")) if isinstance(metadata, dict) else ""
            if not role.startswith(_INTERACTION_ROLE_PREFIX):
                retained.append(raw_stage)
                continue
            task = raw_stage.get("task")
            restarted_stages.append(
                {
                    "stage_id": _normalize_text(raw_stage.get("stage_id")),
                    "previous_status": _normalize_text(raw_stage.get("status")),
                    "previous_task_status": (
                        _normalize_text(task.get("status")) if isinstance(task, dict) else ""
                    ),
                    "engine": "orca",
                    "action": "retired_disabled_interaction_energy",
                }
            )
        payload["stages"] = retained
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict) or not _stage_needs_restart(raw_stage):
            continue
        _apply_flow_restart_settings(
            raw_stage,
            flow_settings,
            restart_allowed_root=restart_allowed_root,
            created_restart_dirs=(
                directory_transaction.created_dirs if directory_transaction is not None else None
            ),
        )
        restarted_stages.append(
            _reset_stage_for_restart(
                raw_stage,
                rematerialize=_stage_should_rematerialize(raw_stage, flow_settings),
            )
        )
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and bool(metadata.get("si_publish_blocked")):
        metadata["si_publish_pending"] = True
        metadata.pop("si_publish_blocked", None)
        metadata.pop("si_publish_attempts", None)
        metadata.pop("si_publish_next_retry_at", None)
        metadata.pop("si_publish_error", None)
        restarted_stages.append(
            {
                "stage_id": "workflow_si",
                "previous_status": "blocked",
                "previous_task_status": "",
                "engine": "workflow",
                "action": "rearmed_si_publication",
            }
        )
    return restarted_stages


def _restart_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    metadata = {}
    payload["metadata"] = metadata
    return metadata


def _apply_restart_summary(
    payload: dict[str, Any],
    *,
    previous_status: str,
    restarted_at: str,
    restarted_stages: list[dict[str, str]],
    flow_settings: dict[str, Any],
) -> None:
    payload["status"] = "planned"
    metadata = _restart_metadata(payload)
    metadata.pop("workflow_error", None)
    _clear_phase_notification_state(metadata, restarted_stages)
    metadata["final_child_sync_pending"] = False
    metadata["final_child_sync_completed_at"] = ""
    metadata["last_restarted_at"] = restarted_at
    metadata["restart_summary"] = {
        "status": "restarted",
        "previous_status": previous_status,
        "restarted_at": restarted_at,
        "restarted_count": len(restarted_stages),
        "flow_manifest_applied": bool(flow_settings.get("applied")),
        "stages": restarted_stages,
    }


def _build_restart_mutation(
    *,
    root: Path,
    workspace: Path,
    payload: dict[str, Any],
    previous_status: str,
    restarted_at: str,
    restarted_stages: list[dict[str, str]],
    flow_settings: dict[str, Any],
    directory_transaction: RestartDirectoryTransaction | None = None,
) -> WorkflowRestartMutation:
    try:
        write_workflow_payload(workspace, payload)
    except Exception:
        # atomic_write_text can report a parent-directory fsync failure after
        # os.replace already made the new workflow visible.  In that case the
        # restart directories are committed references and must not be removed.
        try:
            visible_payload = load_workflow_payload(workspace)
        except Exception:  # noqa: BLE001
            if directory_transaction is not None:
                directory_transaction.mark_payload_outcome_ambiguous()
        else:
            payload_is_visible = visible_payload == payload
            original_is_visible = (
                directory_transaction is not None
                and directory_transaction.original_payload is not None
                and visible_payload == directory_transaction.original_payload
            )
            if payload_is_visible and directory_transaction is not None:
                directory_transaction.mark_payload_committed()
            elif not original_is_visible and directory_transaction is not None:
                directory_transaction.mark_payload_outcome_ambiguous()
        raise
    if directory_transaction is not None:
        directory_transaction.mark_payload_committed()
    sync_workflow_registry(root, workspace, payload)
    return WorkflowRestartMutation(
        root=root,
        workspace=workspace,
        payload=payload,
        previous_status=previous_status,
        restarted_at=restarted_at,
        restarted_stages=restarted_stages,
        flow_manifest_applied=bool(flow_settings.get("applied")),
        summary=workflow_summary(workspace, payload),
    )
