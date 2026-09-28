from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .execution_binding import verify_orca_execution_snapshot
from .types import RunState


@dataclass(frozen=True)
class RunExecutionContext:
    """One claimed queue generation's inputs, built once by the worker child.

    ``resource_request`` is the submitted request that sizes the run, and
    ``admission_token`` names the slot the parent reserved and attached to this
    child; an empty token is refused before execution.
    """

    cfg: AppConfig
    reaction_dir: Path
    selected_inp: Path
    source_selected_inp: str
    selected_input_xyz: str
    resource_request: dict[str, int]
    execution_snapshot: dict[str, Any]
    execution_provenance: dict[str, Any]
    orca_executable: str
    admission_root: Path
    admission_token: str
    admission_app_name: str | None
    admission_task_id: str | None
    queue_id: str | None
    queue_generation: str | None

    def verify_snapshot(self, *, allow_runtime_outputs: bool) -> tuple[Path, str]:
        return verify_orca_execution_snapshot(
            self.reaction_dir,
            self.execution_snapshot,
            expected_selected_inp=self.selected_inp,
            expected_source_selected_inp=self.source_selected_inp,
            expected_selected_input_xyz=self.selected_input_xyz,
            expected_resource_request=self.resource_request,
            allow_runtime_outputs=allow_runtime_outputs,
        )


def bind_queue_identity(state: RunState, context: RunExecutionContext) -> bool:
    """Stamp the run's provenance and queue identity on ``state``; True when it changed.

    The terminal replay binds the run's artifacts to this queue generation
    through these fields.
    """
    changed = False
    provenance = context.execution_provenance
    if provenance and state.get("execution_provenance") != dict(provenance):
        state["execution_provenance"] = dict(provenance)
        changed = True
    task_id = str(context.admission_task_id or "").strip()
    if task_id and state.get("job_id") != task_id:
        state["job_id"] = task_id
        changed = True
    queue_id = str(context.queue_id or "").strip()
    if queue_id and state.get("queue_id") != queue_id:
        state["queue_id"] = queue_id
        changed = True
    queue_generation = str(context.queue_generation or "").strip()
    if queue_generation and state.get("queue_generation") != queue_generation:
        state["queue_generation"] = queue_generation
        changed = True
    return changed


__all__ = ["RunExecutionContext", "bind_queue_identity"]
