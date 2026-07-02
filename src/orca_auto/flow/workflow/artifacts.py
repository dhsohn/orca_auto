from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import (
    coerce_list as _coerce_sequence,
)
from orca_auto.core.utils import (
    coerce_mapping as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)

from .store import WORKFLOW_FILE_NAME, load_workflow_payload


class _WorkflowArtifactRows:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.rows: list[dict[str, Any]] = []
        self.seen: set[tuple[str, str, str, str]] = set()

    def add(
        self,
        *,
        kind: str,
        path_value: Any,
        source: str,
        stage_id: str = "",
        selected: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        path_text = _normalize_text(path_value)
        if not path_text:
            return
        candidate = Path(path_text).expanduser()
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace / candidate).resolve()
        )
        key = (_normalize_text(kind), path_text, _normalize_text(stage_id), _normalize_text(source))
        if key in self.seen:
            return
        self.seen.add(key)
        self.rows.append(
            {
                "kind": _normalize_text(kind) or "artifact",
                "path": path_text,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
                "is_dir": resolved.is_dir(),
                "selected": bool(selected),
                "stage_id": _normalize_text(stage_id),
                "source": _normalize_text(source),
                "metadata": dict(metadata or {}),
            }
        )


def _collect_request_artifacts(collector: _WorkflowArtifactRows, data: dict[str, Any]) -> None:
    request = _coerce_mapping(_coerce_mapping(data.get("metadata")).get("request"))
    for artifact in _coerce_sequence(request.get("source_artifacts")):
        item = _coerce_mapping(artifact)
        collector.add(
            kind=_normalize_text(item.get("kind")) or "source_artifact",
            path_value=item.get("path"),
            source="request.source_artifacts",
            selected=bool(item.get("selected", False)),
            metadata=_coerce_mapping(item.get("metadata")),
        )


def _collect_stage_declared_artifacts(
    collector: _WorkflowArtifactRows,
    *,
    stage: dict[str, Any],
    stage_id: str,
    field_name: str,
    fallback_kind: str,
    source: str,
) -> None:
    for artifact in _coerce_sequence(stage.get(field_name)):
        item = _coerce_mapping(artifact)
        collector.add(
            kind=_normalize_text(item.get("kind")) or fallback_kind,
            path_value=item.get("path"),
            stage_id=stage_id,
            source=source,
            selected=bool(item.get("selected", False)),
            metadata=_coerce_mapping(item.get("metadata")),
        )


def _collect_stage_runtime_artifacts(
    collector: _WorkflowArtifactRows,
    *,
    stage: dict[str, Any],
    stage_id: str,
) -> None:
    task = _coerce_mapping(stage.get("task"))
    task_payload = _coerce_mapping(task.get("payload"))
    enqueue_payload = _coerce_mapping(task.get("enqueue_payload"))
    stage_metadata = _coerce_mapping(stage.get("metadata"))
    collector.add(
        kind="selected_input_xyz",
        path_value=task_payload.get("selected_input_xyz"),
        stage_id=stage_id,
        source="task.payload",
    )
    collector.add(
        kind="selected_inp",
        path_value=task_payload.get("selected_inp") or enqueue_payload.get("selected_inp"),
        stage_id=stage_id,
        source="task.payload",
    )
    collector.add(
        kind="reaction_dir",
        path_value=task_payload.get("reaction_dir") or enqueue_payload.get("reaction_dir"),
        stage_id=stage_id,
        source="task.payload",
    )
    collector.add(
        kind="latest_known_path",
        path_value=stage_metadata.get("latest_known_path"),
        stage_id=stage_id,
        source="stage.metadata",
    )
    collector.add(
        kind="organized_output_dir",
        path_value=stage_metadata.get("organized_output_dir"),
        stage_id=stage_id,
        source="stage.metadata",
    )
    collector.add(
        kind="last_out_path",
        path_value=task_payload.get("last_out_path"),
        stage_id=stage_id,
        source="task.payload",
    )
    collector.add(
        kind="optimized_xyz_path",
        path_value=task_payload.get("optimized_xyz_path")
        or stage_metadata.get("optimized_xyz_path"),
        stage_id=stage_id,
        source="task.payload",
    )


def _collect_stage_artifacts(collector: _WorkflowArtifactRows, data: dict[str, Any]) -> None:
    for raw_stage in _coerce_sequence(data.get("stages")):
        stage = _coerce_mapping(raw_stage)
        stage_id = _normalize_text(stage.get("stage_id"))
        _collect_stage_declared_artifacts(
            collector,
            stage=stage,
            stage_id=stage_id,
            field_name="input_artifacts",
            fallback_kind="input_artifact",
            source="stage.input_artifacts",
        )
        _collect_stage_declared_artifacts(
            collector,
            stage=stage,
            stage_id=stage_id,
            field_name="output_artifacts",
            fallback_kind="output_artifact",
            source="stage.output_artifacts",
        )
        _collect_stage_runtime_artifacts(collector, stage=stage, stage_id=stage_id)


def _collect_metadata_artifacts(collector: _WorkflowArtifactRows, data: dict[str, Any]) -> None:
    metadata = _coerce_mapping(data.get("metadata"))
    precomplex_handoff = _coerce_mapping(metadata.get("precomplex_handoff"))
    collector.add(
        kind="precomplex_handoff_xyz",
        path_value=precomplex_handoff.get("reactant_xyz"),
        source="metadata.precomplex_handoff",
        selected=True,
        metadata={"role": "reactant"},
    )
    collector.add(
        kind="precomplex_handoff_xyz",
        path_value=precomplex_handoff.get("product_xyz"),
        source="metadata.precomplex_handoff",
        selected=True,
        metadata={"role": "product"},
    )
    downstream = _coerce_mapping(metadata.get("downstream_reaction_workflow"))
    downstream_workspace = _normalize_text(downstream.get("workspace_dir"))
    if downstream_workspace:
        collector.add(
            kind="downstream_workflow_workspace",
            path_value=downstream_workspace,
            source="metadata.downstream_reaction_workflow",
            metadata={"workflow_id": _normalize_text(downstream.get("workflow_id"))},
        )
        collector.add(
            kind="downstream_workflow_file",
            path_value=str(Path(downstream_workspace).expanduser() / WORKFLOW_FILE_NAME),
            source="metadata.downstream_reaction_workflow",
            metadata={"workflow_id": _normalize_text(downstream.get("workflow_id"))},
        )
    latest_stage = _coerce_mapping(downstream.get("latest_stage"))
    collector.add(
        kind="downstream_latest_known_path",
        path_value=latest_stage.get("latest_known_path"),
        source="metadata.downstream_reaction_workflow",
        metadata={"stage_id": _normalize_text(latest_stage.get("stage_id"))},
    )
    collector.add(
        kind="downstream_organized_output_dir",
        path_value=latest_stage.get("organized_output_dir"),
        source="metadata.downstream_reaction_workflow",
        metadata={"stage_id": _normalize_text(latest_stage.get("stage_id"))},
    )


def workflow_artifacts(
    workspace_dir: str | Path,
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    workspace = Path(workspace_dir).expanduser().resolve()
    data = payload if payload is not None else load_workflow_payload(workspace)
    collector = _WorkflowArtifactRows(workspace)
    _collect_request_artifacts(collector, data)
    _collect_stage_artifacts(collector, data)
    _collect_metadata_artifacts(collector, data)
    return collector.rows


__all__ = ["workflow_artifacts"]
