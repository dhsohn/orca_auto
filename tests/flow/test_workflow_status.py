from __future__ import annotations

from orca_auto.core.statuses import is_workflow_terminal_status, normalize_status
from orca_auto.flow.workflow import status as workflow_status


def test_workflow_status_helpers_cover_terminal_attention_and_current_stage_selection() -> None:
    assert normalize_status(None) == ""
    assert is_workflow_terminal_status("cancel_failed") is True
    assert (
        workflow_status.workflow_stage_is_terminal(
            {"status": "submission_failed", "task_status": "submission_failed"}
        )
        is True
    )
    assert (
        workflow_status.workflow_stage_is_terminal(
            {"status": "submission_failed", "task_status": "running"}
        )
        is False
    )
    assert workflow_status.workflow_stage_is_terminal({"status": "completed"}) is True
    assert (
        workflow_status.workflow_stage_is_terminal(
            {"status": "completed", "task_status": "unknown"}
        )
        is True
    )
    assert workflow_status.select_current_stage([]) == {}
    assert workflow_status.select_current_stage(
        [
            "not-a-stage",
            {
                "stage_id": "submit",
                "status": "submission_failed",
                "task_status": "submission_failed",
            },
            {"stage_id": "xtb", "status": "running", "task_status": "running"},
        ]
    ) == {"stage_id": "xtb", "status": "running", "task_status": "running"}
    assert workflow_status.select_current_stage(
        [
            {"stage_id": "taskless", "status": "completed", "task_status": "unknown"},
            {"stage_id": "orca", "status": "running", "task_status": "running"},
        ]
    ) == {"stage_id": "orca", "status": "running", "task_status": "running"}
    assert workflow_status.select_current_stage(
        [{"stage_id": "done", "status": "completed", "task_status": "completed"}]
    ) == {"stage_id": "done", "status": "completed", "task_status": "completed"}
