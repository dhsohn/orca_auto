from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from ..engine_options import WorkflowEngineOptions


class StageTransitionContext(TypedDict):
    previous_stage_status: str
    current_stage_status: str
    previous_handoff_status: str
    current_handoff_status: str
    stage_id: str
    engine: str
    task_kind: str


class WorkflowJournalEventPayload(TypedDict, total=False):
    event_type: str
    workflow_id: str
    template_name: str
    status: str
    previous_status: str
    reason: str
    worker_session_id: str
    stage_id: str
    engine: str
    task_kind: str
    stage_status: str
    previous_stage_status: str
    reaction_handoff_status: str
    previous_reaction_handoff_status: str
    metadata: dict[str, Any]


class WorkflowAdvanceResult(TypedDict, total=False):
    workflow_id: str
    template_name: str
    previous_status: str
    status: str
    advanced: bool
    changed: bool
    reason: str
    stage_count: int


class WorkflowRegistryCyclePayload(TypedDict):
    workflow_root: str
    worker_session_id: str
    cycle_started_at: str
    cycle_finished_at: str
    refresh_registry: bool
    submit_ready: bool
    requested_submit_ready: bool
    admission_blocked: bool
    discovered_count: int
    advanced_count: int
    skipped_count: int
    failed_count: int
    workflow_results: list[WorkflowAdvanceResult]


@dataclass(frozen=True)
class WorkflowRuntimeContext:
    root: Path
    options: WorkflowEngineOptions
    submit_ready: bool = True
    refresh_registry: bool = False
    worker_session_id: str = ""
    interval_seconds: float | None = None
    lease_seconds: float = 60.0


@dataclass(frozen=True)
class WorkflowRegistryAdvanceRequest:
    workflow_root: str | Path
    options: WorkflowEngineOptions
    submit_ready: bool = True
    refresh_registry: bool = False
    worker_session_id: str = ""
    interval_seconds: float | None = None
    lease_seconds: float = 60.0

    @classmethod
    def from_values(
        cls,
        *,
        workflow_root: str | Path,
        shared_config: str | None = None,
        crest_config: str | None = None,
        xtb_config: str | None = None,
        orca_config: str | None = None,
        orca_repo_root: str | None = None,
        submit_ready: bool = True,
        refresh_registry: bool = False,
        worker_session_id: str = "",
        interval_seconds: float | None = None,
        lease_seconds: float = 60.0,
    ) -> WorkflowRegistryAdvanceRequest:
        return cls(
            workflow_root=workflow_root,
            options=WorkflowEngineOptions.from_values(
                shared_config=shared_config,
                crest_config=crest_config,
                xtb_config=xtb_config,
                orca_config=orca_config,
                orca_repo_root=orca_repo_root,
            ),
            submit_ready=submit_ready,
            refresh_registry=refresh_registry,
            worker_session_id=worker_session_id,
            interval_seconds=interval_seconds,
            lease_seconds=lease_seconds,
        )

    def runtime_context(self) -> WorkflowRuntimeContext:
        return WorkflowRuntimeContext(
            root=Path(self.workflow_root).expanduser().resolve(),
            options=self.options,
            worker_session_id=self.worker_session_id,
            submit_ready=self.submit_ready,
            refresh_registry=self.refresh_registry,
            interval_seconds=self.interval_seconds,
            lease_seconds=self.lease_seconds,
        )


@dataclass(frozen=True)
class _WorkflowCycle:
    root: Path
    cycle_started_at: str
    session_id: str
    requested_submit_ready: bool
    cycle_submit_ready: bool
    admission_blocked: bool
    lease_expires_at: str


@dataclass(frozen=True)
class _WorkflowCycleProgress:
    workflow_results: list[WorkflowAdvanceResult]
    advanced_count: int
    skipped_count: int
    failed_count: int
