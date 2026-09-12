from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OrcaArtifactContract:
    run_id: str
    status: str
    reason: str
    state_status: str
    reaction_dir: str
    latest_known_path: str
    optimized_xyz_path: str = ""
    queue_id: str = ""
    queue_status: str = ""
    cancel_requested: bool = False
    selected_inp: str = ""
    selected_input_xyz: str = ""
    analyzer_status: str = ""
    completed_at: str = ""
    last_out_path: str = ""
    run_state_path: str = ""
    report_json_path: str = ""
    attempt_count: int = 0
    attempts: tuple[dict[str, Any], ...] = ()
    final_result: dict[str, Any] = field(default_factory=dict)
    resource_request: dict[str, int] = field(default_factory=dict)
    resource_actual: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "state_status": self.state_status,
            "reaction_dir": self.reaction_dir,
            "latest_known_path": self.latest_known_path,
            "optimized_xyz_path": self.optimized_xyz_path,
            "queue_id": self.queue_id,
            "queue_status": self.queue_status,
            "cancel_requested": self.cancel_requested,
            "selected_inp": self.selected_inp,
            "selected_input_xyz": self.selected_input_xyz,
            "analyzer_status": self.analyzer_status,
            "completed_at": self.completed_at,
            "last_out_path": self.last_out_path,
            "run_state_path": self.run_state_path,
            "report_json_path": self.report_json_path,
            "attempt_count": self.attempt_count,
            "attempts": [dict(item) for item in self.attempts],
            "final_result": dict(self.final_result),
            "resource_request": dict(self.resource_request),
            "resource_actual": dict(self.resource_actual),
        }


__all__ = [
    "OrcaArtifactContract",
]
