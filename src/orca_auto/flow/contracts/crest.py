from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orca_auto.core.utils.coercion import normalize_text

from ..xyz_utils import load_xyz_frames
from .xtb import WorkflowStageInput


@dataclass(frozen=True)
class CrestArtifactContract:
    job_id: str
    mode: str
    status: str
    reason: str
    job_dir: str
    latest_known_path: str
    molecule_key: str = ""
    selected_input_xyz: str = ""
    retained_conformer_count: int = 0
    retained_conformer_paths: tuple[str, ...] = ()
    resource_request: dict[str, int] = field(default_factory=dict)
    resource_actual: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "mode": self.mode,
            "status": self.status,
            "reason": self.reason,
            "job_dir": self.job_dir,
            "latest_known_path": self.latest_known_path,
            "molecule_key": self.molecule_key,
            "selected_input_xyz": self.selected_input_xyz,
            "retained_conformer_count": self.retained_conformer_count,
            "retained_conformer_paths": list(self.retained_conformer_paths),
            "resource_request": dict(self.resource_request),
            "resource_actual": dict(self.resource_actual),
        }


@dataclass(frozen=True)
class CrestDownstreamPolicy:
    max_candidates: int = 3

    @classmethod
    def build(cls, *, max_candidates: int = 3) -> CrestDownstreamPolicy:
        return cls(max_candidates=max(1, int(max_candidates)))


def _crest_stage_input(
    contract: CrestArtifactContract,
    *,
    rank: int,
    artifact_path: str,
    metadata: dict[str, Any],
) -> WorkflowStageInput:
    return WorkflowStageInput(
        source_job_id=contract.job_id,
        source_job_type=f"crest_{contract.mode}",
        reaction_key=contract.molecule_key,
        selected_input_xyz=contract.selected_input_xyz,
        rank=rank,
        kind="crest_conformer",
        artifact_path=artifact_path,
        selected=rank == 1,
        metadata=metadata,
    )


def _crest_frame_metadata(
    *,
    contract: CrestArtifactContract,
    artifact_path: str,
    frame: Any,
    frame_count: int,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "mode": contract.mode,
        "source_artifact_path": artifact_path,
        "source_frame_index": frame.index,
        "source_frame_count": frame_count,
    }
    if frame.energy is not None:
        metadata["source_frame_energy"] = frame.energy
    return metadata


def _crest_conformer_inputs_for_path(
    contract: CrestArtifactContract,
    *,
    artifact_path: str,
    start_rank: int,
    max_candidates: int,
) -> tuple[WorkflowStageInput, ...]:
    frames = load_xyz_frames(artifact_path)
    if len(frames) <= 1:
        return (
            _crest_stage_input(
                contract,
                rank=start_rank,
                artifact_path=artifact_path,
                metadata={"mode": contract.mode},
            ),
        )

    rows: list[WorkflowStageInput] = []
    frame_count = len(frames)
    for offset, frame in enumerate(frames):
        rank = start_rank + offset
        rows.append(
            _crest_stage_input(
                contract,
                rank=rank,
                artifact_path=artifact_path,
                metadata=_crest_frame_metadata(
                    contract=contract,
                    artifact_path=artifact_path,
                    frame=frame,
                    frame_count=frame_count,
                ),
            )
        )
        if len(rows) >= max_candidates:
            break
    return tuple(rows)


def to_workflow_stage_inputs(
    contract: CrestArtifactContract,
    *,
    policy: CrestDownstreamPolicy | None = None,
) -> tuple[WorkflowStageInput, ...]:
    active_policy = policy or CrestDownstreamPolicy.build()
    rows: list[WorkflowStageInput] = []
    next_rank = 1
    for path in contract.retained_conformer_paths:
        text = normalize_text(path, none="None")
        if not text:
            continue
        for stage_input in _crest_conformer_inputs_for_path(
            contract,
            artifact_path=text,
            start_rank=next_rank,
            max_candidates=active_policy.max_candidates - len(rows),
        ):
            rows.append(stage_input)
            next_rank += 1
            if len(rows) >= active_policy.max_candidates:
                break
        if len(rows) >= active_policy.max_candidates:
            break
    return tuple(rows)


__all__ = [
    "CrestArtifactContract",
    "CrestDownstreamPolicy",
    "WorkflowStageInput",
    "to_workflow_stage_inputs",
]
