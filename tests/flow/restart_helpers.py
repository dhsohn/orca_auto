from __future__ import annotations

import json
from pathlib import Path


def _write_workflow(workspace: Path, payload: dict[str, object]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")


def _failed_orca_restart_stage(stage_id: str, reaction_dir: Path) -> dict[str, object]:
    selected_inp = reaction_dir / "input.inp"
    selected_xyz = reaction_dir / "input.xyz"
    return {
        "stage_id": stage_id,
        "stage_kind": "orca_stage",
        "status": "failed",
        "task": {
            "engine": "orca",
            "task_kind": "optts_freq",
            "status": "failed",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "selected_input_xyz": str(selected_xyz),
            },
            "enqueue_payload": {
                "submitter": "orca_auto_orca",
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
        },
        "metadata": {},
    }
