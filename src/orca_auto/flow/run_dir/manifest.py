from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.cli_common import _normalize_workflow_type
from orca_auto.core.utils.coercion import normalize_text

from ..manifest import (
    FLOW_MANIFEST_FILENAMES as WORKFLOW_MANIFEST_FILENAMES,
)
from ..manifest import (
    load_flow_manifest as _shared_load_flow_manifest,
)
from ..manifest import (
    manifest_allows_external_inputs as _shared_manifest_allows_external_inputs,
)
from ..manifest import (
    manifest_mapping as _shared_manifest_mapping,
)
from ..manifest import (
    resolve_endpoint_pairing_manifest as _shared_resolve_endpoint_pairing_manifest,
)
from ..manifest import (
    resolve_engine_manifest as _shared_resolve_engine_manifest,
)
from ..manifest import (
    resolve_manifest_file_value as _shared_resolve_manifest_file_value,
)
from .layout import (
    STANDARD_CONFORMER_INPUT_FILENAME,
    STANDARD_REACTION_PRODUCT_FILENAME,
    STANDARD_REACTION_REACTANT_FILENAME,
    inspect_workflow_run_dir,
)
from .options import RunDirManifestSections, RunDirWorkflowConfig


def _load_run_dir_manifest(workflow_dir: Path) -> dict[str, Any]:
    return _shared_load_flow_manifest(
        workflow_dir,
        filenames=tuple(WORKFLOW_MANIFEST_FILENAMES),
        description="Run directory manifest",
    )


def _resolve_run_dir_path(
    workflow_dir: Path,
    *,
    explicit: Any,
    manifest: dict[str, Any],
    key: str,
    default_names: tuple[str, ...],
) -> str:
    candidate_text = normalize_text(explicit)
    if candidate_text:
        return _shared_resolve_manifest_file_value(
            workflow_dir,
            candidate_text,
            allow_external_inputs=True,
            field_name=key,
        )

    candidate_text = normalize_text(manifest.get(key))
    if candidate_text:
        return _shared_resolve_manifest_file_value(
            workflow_dir,
            candidate_text,
            allow_external_inputs=_shared_manifest_allows_external_inputs(manifest),
            field_name=key,
        )

    for name in default_names:
        candidate = workflow_dir / name
        if candidate.exists():
            return str(candidate.resolve())
    return ""


def _resolve_run_dir_workflow_type(
    args: Any, manifest: dict[str, Any], workflow_layout: Any
) -> str:
    workflow_type_text = normalize_text(getattr(args, "workflow_type", None))
    if not workflow_type_text:
        workflow_type_text = normalize_text(manifest.get("workflow_type"))
    if workflow_type_text:
        return _normalize_workflow_type(workflow_type_text)
    if workflow_layout.is_ambiguous:
        raise ValueError(
            "Ambiguous workflow_dir: found both reaction inputs and conformer input. "
            "Set workflow_type in flow.yaml to choose one."
        )
    inferred_workflow_type = workflow_layout.inferred_workflow_type
    if inferred_workflow_type:
        return inferred_workflow_type
    raise ValueError(
        "Could not infer workflow type from workflow_dir. "
        "Expected reactant.xyz + product.xyz or input.xyz."
    )


def _resolve_run_dir_manifest_sections(
    workflow_dir: Path, manifest: dict[str, Any]
) -> RunDirManifestSections:
    xtb_manifest = _shared_resolve_engine_manifest(workflow_dir, manifest, "xtb")
    return RunDirManifestSections(
        resources=_shared_manifest_mapping(manifest.get("resources")),
        crest=_shared_resolve_engine_manifest(workflow_dir, manifest, "crest"),
        xtb=xtb_manifest,
        endpoint_pairing=_shared_resolve_endpoint_pairing_manifest(manifest, xtb_manifest),
        orca=_shared_resolve_engine_manifest(workflow_dir, manifest, "orca"),
    )


def _load_run_dir_workflow_config(args: Any, workflow_dir: Path) -> RunDirWorkflowConfig:
    workflow_layout = inspect_workflow_run_dir(workflow_dir)
    if not workflow_layout.has_manifest:
        raise ValueError("workflow run-dir requires flow.yaml in workflow_dir.")

    manifest = _load_run_dir_manifest(workflow_dir)
    sections = _resolve_run_dir_manifest_sections(workflow_dir, manifest)
    return RunDirWorkflowConfig(
        workflow_dir=workflow_dir,
        manifest=manifest,
        sections=sections,
        reactant_xyz=_resolve_run_dir_path(
            workflow_dir,
            explicit=getattr(args, "reactant_xyz", None),
            manifest=manifest,
            key="reactant_xyz",
            default_names=(STANDARD_REACTION_REACTANT_FILENAME,),
        ),
        product_xyz=_resolve_run_dir_path(
            workflow_dir,
            explicit=getattr(args, "product_xyz", None),
            manifest=manifest,
            key="product_xyz",
            default_names=(STANDARD_REACTION_PRODUCT_FILENAME,),
        ),
        input_xyz=_resolve_run_dir_path(
            workflow_dir,
            explicit=getattr(args, "input_xyz", None),
            manifest=manifest,
            key="input_xyz",
            default_names=(STANDARD_CONFORMER_INPUT_FILENAME,),
        ),
        workflow_type=_resolve_run_dir_workflow_type(args, manifest, workflow_layout),
    )


__all__ = [
    "WORKFLOW_MANIFEST_FILENAMES",
]
