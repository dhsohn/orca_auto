from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import (
    validate_workflow_id_path_segment,
    workflow_root_for_workspace,
)
from orca_auto.core.utils.coercion import normalize_text

from ..orchestration import create_conformer_screening_workflow
from ..restart import restart_failed_workflow
from ..run_dir import manifest as _run_dir_manifest
from ..run_dir import options as _run_dir_options
from ..templates import DEFAULT_CONFORMER_ORCA_ROUTE_LINE
from . import workflow_output as _workflow_output


def _workflow_root_for_existing_run_dir(args: Any, workflow_dir: Path) -> Path:
    raw_root = normalize_text(getattr(args, "workflow_root", None))
    if raw_root:
        return Path(raw_root).expanduser().resolve()
    return workflow_root_for_workspace(workflow_dir)


def _create_run_dir_workflow(args: Any, workflow_dir: Path) -> dict[str, Any]:
    config = _run_dir_manifest._load_run_dir_workflow_config(args, workflow_dir)
    if not config.input_xyz:
        raise ValueError("conformer_screening requires input.xyz (or manifest/CLI override).")
    workflow_root = _run_dir_options._resolve_required_workflow_root(args, config.manifest)
    if config.workflow_dir == Path(workflow_root).expanduser().resolve():
        raise ValueError(
            "run-dir workflow scaffold cannot be the workflow_root itself; "
            "create the scaffold as a subdirectory of workflow_root"
        )
    options = _run_dir_options._resolve_run_dir_workflow_options(
        args,
        config.manifest,
        config.sections,
        default_orca_route_line=DEFAULT_CONFORMER_ORCA_ROUTE_LINE,
        default_max_orca_stages=20,
        workflow_root=workflow_root,
        workflow_type=config.workflow_type,
    )

    crest_kwargs: dict[str, Any] = (
        {"crest_job_manifest": config.crest_manifest} if config.crest_manifest else {}
    )
    return create_conformer_screening_workflow(
        input_xyz=config.input_xyz,
        # The factory mints a fresh generation inside the scaffold directory.
        scaffold_dir=str(config.workflow_dir),
        workflow_root=options.workflow_root,
        crest_mode=options.crest_mode,
        priority=options.priority,
        max_cores=options.max_cores,
        max_memory_gb=options.max_memory_gb,
        max_orca_stages=options.max_orca_stages,
        orca_route_line=options.orca_route_line,
        charge=options.charge,
        multiplicity=options.multiplicity,
        boltzmann_temperature_k=options.boltzmann_temperature_k,
        interaction_energy=options.interaction_energy,
        rmsd_dedup=options.rmsd_dedup,
        **crest_kwargs,
    )


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
        validate_workflow_id_path_segment(workflow_dir.name)

        if (workflow_dir / "workflow.json").is_file():
            payload = _restart_existing_run_dir_workflow(args, workflow_dir)
            return _workflow_output.emit_restarted_workflow(
                payload, json_mode=bool(getattr(args, "json", False))
            )

        payload = _create_run_dir_workflow(args, workflow_dir)
    except (ValueError, FileExistsError) as exc:
        _workflow_output.emit_error(exc)
        return 1

    return _workflow_output.emit_created_workflow(
        payload, json_mode=bool(getattr(args, "json", False))
    )
