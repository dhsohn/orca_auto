from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import (
    ORCA_AUTO_CLI_COMMAND,
    ORCA_AUTO_CLI_MODULE,
    ORCA_AUTO_ORCA_SUBMITTER,
)
from orca_auto.core.utils import atomic_write_json, normalize_text
from orca_auto.core.utils.coercion import safe_int

from . import _orca_stage_payloads
from .contracts import WorkflowArtifactRef, WorkflowStage, WorkflowStageInput, WorkflowTask
from .xyz_utils import write_orca_ready_xyz


def safe_name(value: str, *, fallback: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in normalize_text(value)
    )
    cleaned = cleaned.strip("._-").lower()
    return cleaned or fallback


def ensure_route_line(route_line: str, *, default: str = "r2scan-3c TightSCF") -> str:
    normalized = normalize_text(route_line) or normalize_text(default)
    return normalized if normalized.startswith("!") else f"! {normalized}"


def maxcore_mb_per_core(*, max_memory_gb: int, max_cores: int) -> int:
    total_mb = max(1, int(max_memory_gb)) * 1024
    return max(1, total_mb // max(1, int(max_cores)))


def _positive_multiplicity(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"multiplicity must be an integer >= 1. got={value!r}") from exc
    if parsed < 1:
        raise ValueError(f"multiplicity must be >= 1. got={parsed}")
    return parsed


def render_orca_input(
    *,
    route_line: str,
    charge: int,
    multiplicity: int,
    max_cores: int,
    max_memory_gb: int,
    xyz_filename: str,
    default_route_line: str = "r2scan-3c TightSCF",
    geom_block: str = "",
) -> str:
    parsed_multiplicity = _positive_multiplicity(multiplicity)
    geom_lines = [*geom_block.splitlines(), ""] if geom_block.strip() else []
    return "\n".join(
        [
            ensure_route_line(route_line, default=default_route_line),
            "",
            "%pal",
            f"  nprocs {max(1, int(max_cores))}",
            "end",
            f"%maxcore {maxcore_mb_per_core(max_memory_gb=max_memory_gb, max_cores=max_cores)}",
            "",
            *geom_lines,
            f"* xyzfile {int(charge)} {parsed_multiplicity} {xyz_filename}",
            "",
        ]
    )


def build_orca_enqueue_payload(
    *,
    workflow_id: str,
    stage_id: str,
    reaction_dir: str,
    selected_inp: str,
    priority: int,
    resource_request: dict[str, int],
    source_job_id: str,
    reaction_key: str,
) -> dict[str, Any]:
    config_placeholder = "<orca_auto_config>"
    resources = _enqueue_resource_values(resource_request)
    command_argv = _orca_enqueue_command_argv(
        config_placeholder=config_placeholder,
        reaction_dir=reaction_dir,
        priority=priority,
        resources=resources,
    )
    command = _orca_enqueue_command(
        config_placeholder=config_placeholder,
        reaction_dir=reaction_dir,
        priority=priority,
        resources=resources,
    )
    return {
        "submitter": ORCA_AUTO_ORCA_SUBMITTER,
        "command": command,
        "command_argv": command_argv,
        "requires_config": True,
        "config_argument_placeholder": config_placeholder,
        "reaction_dir": reaction_dir,
        "selected_inp": selected_inp,
        "priority": int(priority),
        "force": False,
        "max_cores": resources["max_cores"],
        "max_memory_gb": resources["max_memory_gb"],
        "workflow_id": workflow_id,
        "workflow_stage_id": stage_id,
        "source_job_id": source_job_id,
        "reaction_key": reaction_key,
        "resource_request": dict(resource_request),
    }


def _enqueue_resource_values(resource_request: dict[str, int]) -> dict[str, int]:
    return {
        "max_cores": int(resource_request.get("max_cores", 0) or 0),
        "max_memory_gb": int(resource_request.get("max_memory_gb", 0) or 0),
    }


def _resource_override_argv(resources: dict[str, int]) -> list[str]:
    argv: list[str] = []
    if resources["max_cores"] > 0:
        argv.extend(["--max-cores", str(resources["max_cores"])])
    if resources["max_memory_gb"] > 0:
        argv.extend(["--max-memory-gb", str(resources["max_memory_gb"])])
    return argv


def _resource_override_command_parts(resources: dict[str, int]) -> list[str]:
    parts: list[str] = []
    if resources["max_cores"] > 0:
        parts.append(f"--max-cores {resources['max_cores']}")
    if resources["max_memory_gb"] > 0:
        parts.append(f"--max-memory-gb {resources['max_memory_gb']}")
    return parts


def _orca_enqueue_command_argv(
    *,
    config_placeholder: str,
    reaction_dir: str,
    priority: int,
    resources: dict[str, int],
) -> list[str]:
    return [
        "python",
        "-m",
        ORCA_AUTO_CLI_MODULE,
        "--config",
        config_placeholder,
        "run-dir",
        reaction_dir,
        "--priority",
        str(int(priority)),
        *_resource_override_argv(resources),
    ]


def _orca_enqueue_command(
    *,
    config_placeholder: str,
    reaction_dir: str,
    priority: int,
    resources: dict[str, int],
) -> str:
    command_parts = [
        f"{ORCA_AUTO_CLI_COMMAND} --config {config_placeholder}",
        "run-dir",
        f"'{reaction_dir}'",
        f"--priority {int(priority)}",
        *_resource_override_command_parts(resources),
    ]
    return " ".join(command_parts)


@dataclass(frozen=True)
class OrcaStageMaterialization:
    reaction_dir: str
    selected_inp: str
    selected_xyz: str
    stage_workspace_dir: str


@dataclass(frozen=True)
class OrcaStageMaterializationRequest:
    workspace_dir: Path
    stage_root_name: str
    stage_key: str
    source_artifact_path: str
    candidate_kind: str
    route_line: str
    charge: int
    multiplicity: int
    max_cores: int
    max_memory_gb: int
    xyz_filename: str = "input.xyz"
    inp_filename: str = "input.inp"
    source_frame_index: int = 0
    extra_source_payload: dict[str, Any] | None = None
    geom_block: str = ""


@dataclass(frozen=True)
class OrcaStageBuildContext:
    workflow_id: str
    template_name: str
    stage_id: str
    stage_key: str
    stage_root_name: str
    workspace_dir: Path
    input_artifact_kind: str
    candidate: WorkflowStageInput
    task_kind: str
    route_line: str
    charge: int
    multiplicity: int
    max_cores: int
    max_memory_gb: int
    priority: int
    xyz_filename: str
    inp_filename: str
    input_label: str | None = None
    geom_block: str = ""

    @property
    def resource_request(self) -> dict[str, int]:
        return {
            "max_cores": max(1, int(self.max_cores)),
            "max_memory_gb": max(1, int(self.max_memory_gb)),
        }

    @property
    def selected_input_label(self) -> str:
        return self.input_label or Path(self.candidate.artifact_path).name

    def materialization_request(
        self, *, extra_source_payload: dict[str, Any] | None = None
    ) -> OrcaStageMaterializationRequest:
        return OrcaStageMaterializationRequest(
            workspace_dir=self.workspace_dir,
            stage_root_name=self.stage_root_name,
            stage_key=self.stage_key,
            source_artifact_path=self.candidate.artifact_path,
            candidate_kind=self.candidate.kind,
            route_line=self.route_line,
            charge=self.charge,
            multiplicity=self.multiplicity,
            max_cores=self.max_cores,
            max_memory_gb=self.max_memory_gb,
            xyz_filename=self.xyz_filename,
            inp_filename=self.inp_filename,
            source_frame_index=_candidate_source_frame_index(self.candidate),
            extra_source_payload=extra_source_payload,
            geom_block=self.geom_block,
        )


def materialize_orca_stage(
    *,
    workspace_dir: Path,
    stage_root_name: str,
    stage_key: str,
    source_artifact_path: str,
    candidate_kind: str,
    route_line: str,
    charge: int,
    multiplicity: int,
    max_cores: int,
    max_memory_gb: int,
    xyz_filename: str = "input.xyz",
    inp_filename: str = "input.inp",
    source_frame_index: int = 0,
    extra_source_payload: dict[str, Any] | None = None,
) -> OrcaStageMaterialization:
    return materialize_orca_stage_from_request(
        OrcaStageMaterializationRequest(
            workspace_dir=workspace_dir,
            stage_root_name=stage_root_name,
            stage_key=stage_key,
            source_artifact_path=source_artifact_path,
            candidate_kind=candidate_kind,
            route_line=route_line,
            charge=charge,
            multiplicity=multiplicity,
            max_cores=max_cores,
            max_memory_gb=max_memory_gb,
            xyz_filename=xyz_filename,
            inp_filename=inp_filename,
            source_frame_index=source_frame_index,
            extra_source_payload=extra_source_payload,
        )
    )


def _candidate_source_frame_index(candidate: WorkflowStageInput) -> int:
    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    return max(0, safe_int(metadata.get("source_frame_index", 0), default=0))


def materialize_orca_stage_from_request(
    request: OrcaStageMaterializationRequest,
) -> OrcaStageMaterialization:
    source_xyz = Path(request.source_artifact_path).expanduser().resolve()
    if not source_xyz.exists():
        raise FileNotFoundError(f"ORCA stage source artifact not found: {source_xyz}")

    stage_dir = _orca_stage_dir(request)
    reaction_dir = stage_dir
    reaction_dir.mkdir(parents=True, exist_ok=True)

    target_xyz, geometry_metadata = _materialize_orca_geometry(
        request=request,
        source_xyz=source_xyz,
        reaction_dir=reaction_dir,
    )
    target_inp = _write_orca_input_file(
        request=request,
        reaction_dir=reaction_dir,
        xyz_filename=target_xyz.name,
    )
    _write_source_candidate_payload(
        stage_dir=stage_dir,
        source_xyz=source_xyz,
        geometry_metadata=geometry_metadata,
        extra_source_payload=request.extra_source_payload,
    )
    return OrcaStageMaterialization(
        reaction_dir=str(reaction_dir),
        selected_inp=str(target_inp),
        selected_xyz=str(target_xyz),
        stage_workspace_dir=str(stage_dir),
    )


def _orca_stage_dir(request: OrcaStageMaterializationRequest) -> Path:
    root_name = normalize_text(request.stage_root_name)
    stage_root = request.workspace_dir / root_name if root_name else request.workspace_dir
    return stage_root / request.stage_key


def _materialize_orca_geometry(
    *,
    request: OrcaStageMaterializationRequest,
    source_xyz: Path,
    reaction_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    target_xyz = reaction_dir / request.xyz_filename
    geometry_metadata = write_orca_ready_xyz(
        source_path=source_xyz,
        target_path=target_xyz,
        candidate_kind=request.candidate_kind,
        source_frame_index=request.source_frame_index,
    )
    return target_xyz, dict(geometry_metadata)


def _write_orca_input_file(
    *,
    request: OrcaStageMaterializationRequest,
    reaction_dir: Path,
    xyz_filename: str,
) -> Path:
    target_inp = reaction_dir / request.inp_filename
    target_inp.write_text(
        render_orca_input(
            route_line=request.route_line,
            charge=request.charge,
            multiplicity=request.multiplicity,
            max_cores=request.max_cores,
            max_memory_gb=request.max_memory_gb,
            xyz_filename=xyz_filename,
            geom_block=request.geom_block,
        ),
        encoding="utf-8",
    )
    return target_inp


def _write_source_candidate_payload(
    *,
    stage_dir: Path,
    source_xyz: Path,
    geometry_metadata: dict[str, Any],
    extra_source_payload: dict[str, Any] | None,
) -> None:
    source_payload = {
        "source_artifact_path": str(source_xyz),
        "geometry_materialization": dict(geometry_metadata),
        **dict(extra_source_payload or {}),
    }
    atomic_write_json(
        stage_dir / "source_candidate.json", source_payload, ensure_ascii=True, indent=2
    )


def build_materialized_orca_stage(
    *,
    workflow_id: str,
    template_name: str,
    stage_id: str,
    stage_key: str,
    stage_root_name: str,
    workspace_dir: Path,
    input_artifact_kind: str,
    candidate: WorkflowStageInput,
    task_kind: str,
    route_line: str,
    charge: int,
    multiplicity: int,
    max_cores: int,
    max_memory_gb: int,
    priority: int,
    xyz_filename: str,
    inp_filename: str,
    input_label: str | None = None,
    geom_block: str = "",
) -> WorkflowStage:
    return build_materialized_orca_stage_from_context(
        OrcaStageBuildContext(
            workflow_id=workflow_id,
            template_name=template_name,
            stage_id=stage_id,
            stage_key=stage_key,
            stage_root_name=stage_root_name,
            workspace_dir=workspace_dir,
            input_artifact_kind=input_artifact_kind,
            candidate=candidate,
            task_kind=task_kind,
            route_line=route_line,
            charge=charge,
            multiplicity=multiplicity,
            max_cores=max_cores,
            max_memory_gb=max_memory_gb,
            priority=priority,
            xyz_filename=xyz_filename,
            inp_filename=inp_filename,
            input_label=input_label,
            geom_block=geom_block,
        )
    )


def build_materialized_orca_stage_from_context(ctx: OrcaStageBuildContext) -> WorkflowStage:
    resource_request = ctx.resource_request
    materialized = materialize_orca_stage_from_request(
        ctx.materialization_request(
            extra_source_payload=_orca_stage_payloads.candidate_source_payload(ctx.candidate)
        )
    )
    enqueue_payload = build_orca_enqueue_payload(
        workflow_id=ctx.workflow_id,
        stage_id=ctx.stage_id,
        reaction_dir=materialized.reaction_dir,
        selected_inp=materialized.selected_inp,
        priority=ctx.priority,
        resource_request=resource_request,
        source_job_id=ctx.candidate.source_job_id,
        reaction_key=ctx.candidate.reaction_key,
    )
    task_payload = _orca_stage_payloads.task_payload(
        ctx=ctx,
        materialized=materialized,
        resource_request=resource_request,
        cli_command=ORCA_AUTO_CLI_COMMAND,
    )
    task = _orca_stage_payloads.workflow_task(
        ctx=ctx,
        materialized=materialized,
        resource_request=resource_request,
        enqueue_payload=enqueue_payload,
        task_payload=task_payload,
        workflow_task_cls=WorkflowTask,
    )
    atomic_write_json(
        Path(materialized.stage_workspace_dir) / "enqueue_payload.json",
        enqueue_payload,
        ensure_ascii=True,
        indent=2,
    )
    return _orca_stage_payloads.workflow_stage(
        ctx=ctx,
        materialized=materialized,
        task=task,
        workflow_stage_cls=WorkflowStage,
        artifact_ref_cls=WorkflowArtifactRef,
    )


__all__ = [
    "build_materialized_orca_stage",
    "build_materialized_orca_stage_from_context",
    "build_orca_enqueue_payload",
    "ensure_route_line",
    "materialize_orca_stage",
    "materialize_orca_stage_from_request",
    "normalize_text",
    "OrcaStageBuildContext",
    "OrcaStageMaterialization",
    "OrcaStageMaterializationRequest",
    "render_orca_input",
    "safe_name",
]
