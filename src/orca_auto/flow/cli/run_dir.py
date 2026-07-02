from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.utils.coercion import normalize_text

from ..orchestration import (
    create_conformer_screening_workflow,
    create_reaction_ts_search_workflow,
)
from ..restart import restart_failed_workflow
from ..run_dir import manifest as _run_dir_manifest
from ..run_dir import options as _run_dir_options
from . import workflow_output as _workflow_output


@dataclass(frozen=True)
class _RunDirWorkflowCreationSpec:
    workflow_type: str
    required_input_kwargs: tuple[tuple[str, str], ...]
    missing_inputs_error: str
    default_orca_route_line: str
    default_max_orca_stages: int
    option_kwargs: tuple[tuple[str, str], ...] = ()
    manifest_kwargs: tuple[tuple[str, str], ...] = ()


_REACTION_RUN_DIR_WORKFLOW_SPEC = _RunDirWorkflowCreationSpec(
    workflow_type="reaction_ts_search",
    required_input_kwargs=(
        ("reactant_xyz", "reactant_xyz"),
        ("product_xyz", "product_xyz"),
    ),
    missing_inputs_error=(
        "reaction_ts_search requires both reactant.xyz and product.xyz (or manifest/CLI overrides)."
    ),
    default_orca_route_line="! r2scan-3c OptTS Freq TightSCF",
    default_max_orca_stages=3,
    option_kwargs=(
        ("max_crest_candidates", "max_crest_candidates"),
        ("max_xtb_stages", "max_xtb_stages"),
    ),
    manifest_kwargs=(
        ("crest_job_manifest", "crest_manifest"),
        ("xtb_job_manifest", "xtb_manifest"),
        ("endpoint_pairing", "endpoint_pairing"),
    ),
)

_CONFORMER_RUN_DIR_WORKFLOW_SPEC = _RunDirWorkflowCreationSpec(
    workflow_type="conformer_screening",
    required_input_kwargs=(("input_xyz", "input_xyz"),),
    missing_inputs_error="conformer_screening requires input.xyz (or manifest/CLI override).",
    default_orca_route_line="! r2scan-3c Opt TightSCF",
    default_max_orca_stages=20,
    manifest_kwargs=(("crest_job_manifest", "crest_manifest"),),
)


def _safe_workflow_name(value: Any, *, fallback: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in normalize_text(value)
    )
    cleaned = cleaned.strip("._-").lower()
    return cleaned or fallback


def _preferred_run_dir_workflow_id(workflow_dir: Path, *, workflow_type: str) -> str:
    stem = _safe_workflow_name(workflow_dir.name, fallback="workflow")
    prefix = "wf_reaction_ts" if workflow_type == "reaction_ts_search" else "wf_conformer_screening"
    if stem.startswith(prefix):
        return stem
    return f"{prefix}_{stem}"


def _unique_run_dir_workflow_id(
    workflow_dir: Path,
    *,
    workflow_root: str | Path,
    workflow_type: str,
) -> str:
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    if workflow_dir.parent == workflow_root_path and not (workflow_dir / "workflow.json").exists():
        return workflow_dir.name

    preferred = _preferred_run_dir_workflow_id(workflow_dir, workflow_type=workflow_type)
    candidate = preferred
    suffix = 2
    while (workflow_root_path / candidate).exists():
        candidate = f"{preferred}_{suffix:02d}"
        suffix += 1
    return candidate


def _workflow_root_for_existing_run_dir(args: Any, workflow_dir: Path) -> Path:
    raw_root = normalize_text(getattr(args, "workflow_root", None))
    if raw_root:
        return Path(raw_root).expanduser().resolve()
    return workflow_dir.parent


def _update_present_kwargs(kwargs: dict[str, Any], values: dict[str, Any]) -> None:
    for key, value in values.items():
        if value:
            kwargs[key] = value


def _run_dir_required_input_kwargs(
    config: _run_dir_options.RunDirWorkflowConfig,
    spec: _RunDirWorkflowCreationSpec,
) -> dict[str, Any]:
    workflow_kwargs: dict[str, Any] = {}
    for kwarg_name, config_attr in spec.required_input_kwargs:
        value = getattr(config, config_attr)
        if not value:
            raise ValueError(spec.missing_inputs_error)
        workflow_kwargs[kwarg_name] = value
    return workflow_kwargs


def _run_dir_option_kwargs(
    options: _run_dir_options.RunDirWorkflowOptions,
    spec: _RunDirWorkflowCreationSpec,
) -> dict[str, Any]:
    return {
        kwarg_name: getattr(options, option_attr) for kwarg_name, option_attr in spec.option_kwargs
    }


def _run_dir_manifest_kwargs(
    config: _run_dir_options.RunDirWorkflowConfig,
    spec: _RunDirWorkflowCreationSpec,
) -> dict[str, Any]:
    return {
        kwarg_name: getattr(config, config_attr) for kwarg_name, config_attr in spec.manifest_kwargs
    }


def _run_dir_workflow_kwargs(
    args: Any,
    config: _run_dir_options.RunDirWorkflowConfig,
    spec: _RunDirWorkflowCreationSpec,
) -> dict[str, Any]:
    workflow_kwargs = _run_dir_required_input_kwargs(config, spec)
    workflow_root = _run_dir_options._resolve_required_workflow_root(args, config.manifest)
    options, common_kwargs = _run_dir_options._resolve_run_dir_workflow_option_bundle(
        args,
        config.manifest,
        config.sections,
        default_orca_route_line=spec.default_orca_route_line,
        default_max_orca_stages=spec.default_max_orca_stages,
        workflow_root=workflow_root,
    )

    workflow_kwargs.update(
        {
            "workflow_id": _unique_run_dir_workflow_id(
                config.workflow_dir,
                workflow_root=workflow_root,
                workflow_type=config.workflow_type,
            ),
            **common_kwargs,
        }
    )
    workflow_kwargs.update(_run_dir_option_kwargs(options, spec))
    _update_present_kwargs(workflow_kwargs, _run_dir_manifest_kwargs(config, spec))
    return workflow_kwargs


def _create_reaction_run_dir_workflow(
    args: Any, config: _run_dir_options.RunDirWorkflowConfig
) -> dict[str, Any]:
    workflow_kwargs = _run_dir_workflow_kwargs(args, config, _REACTION_RUN_DIR_WORKFLOW_SPEC)
    return create_reaction_ts_search_workflow(**workflow_kwargs)


def _create_conformer_run_dir_workflow(
    args: Any, config: _run_dir_options.RunDirWorkflowConfig
) -> dict[str, Any]:
    workflow_kwargs = _run_dir_workflow_kwargs(args, config, _CONFORMER_RUN_DIR_WORKFLOW_SPEC)
    return create_conformer_screening_workflow(**workflow_kwargs)


def _create_run_dir_workflow(args: Any, workflow_dir: Path) -> dict[str, Any]:
    config = _run_dir_manifest._load_run_dir_workflow_config(args, workflow_dir)
    if config.workflow_type == _REACTION_RUN_DIR_WORKFLOW_SPEC.workflow_type:
        return _create_reaction_run_dir_workflow(args, config)
    return _create_conformer_run_dir_workflow(args, config)


def _restart_existing_run_dir_workflow(args: Any, workflow_dir: Path) -> dict[str, Any]:
    return restart_failed_workflow(
        workspace_dir=workflow_dir,
        workflow_root=_workflow_root_for_existing_run_dir(args, workflow_dir),
        force=bool(getattr(args, "force", False)),
    )


def cmd_run_dir(args: Any) -> int:
    try:
        workflow_dir = Path(args.workflow_dir).expanduser().resolve()
        if not workflow_dir.is_dir():
            raise ValueError(f"workflow_dir does not exist or is not a directory: {workflow_dir}")

        if (workflow_dir / "workflow.json").is_file():
            payload = _restart_existing_run_dir_workflow(args, workflow_dir)
            return _workflow_output.emit_restarted_workflow(
                payload, json_mode=bool(getattr(args, "json", False))
            )

        payload = _create_run_dir_workflow(args, workflow_dir)
    except ValueError as exc:
        _workflow_output.emit_error(exc)
        return 1

    return _workflow_output.emit_created_workflow(
        payload, json_mode=bool(getattr(args, "json", False))
    )
