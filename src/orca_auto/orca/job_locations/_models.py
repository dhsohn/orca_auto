from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord


@dataclass(frozen=True)
class JobArtifactContext:
    record: JobLocationRecord | None = None
    job_dir: Path | None = None
    state: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    organized_ref: dict[str, Any] | None = None


@dataclass(frozen=True)
class JobRuntimeContext:
    artifact: JobArtifactContext = field(default_factory=JobArtifactContext)
    queue_entry: dict[str, Any] | None = None
    organized_dir: Path | None = None


@dataclass(frozen=True)
class OrcaContractPayloadContext:
    runtime: JobRuntimeContext
    target: str
    reaction_dir: str
    record: JobLocationRecord | None
    queue_entry: dict[str, Any]
    state: dict[str, Any]
    report: dict[str, Any]
    organized_ref: dict[str, Any]
    current_dir: Path | None
    resolved_run_id: str
    latest_known_path: str
    state_status: str
    status: str
    analyzer_status: str
    reason: str
    completed_at: str
    selected_inp: str
    selected_input_xyz: str
    last_out_path: str
    optimized_xyz_path: str
    organized_output_dir: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]

    @property
    def missing(self) -> bool:
        return self.record is None and self.current_dir is None and not self.queue_entry


@dataclass(frozen=True)
class OrcaContractResolvedFields:
    resolved_run_id: str
    latest_known_path: str
    state_status: str
    status: str
    analyzer_status: str
    reason: str
    completed_at: str
    selected_inp: str
    selected_input_xyz: str
    last_out_path: str
    optimized_xyz_path: str
    organized_output_dir: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]
