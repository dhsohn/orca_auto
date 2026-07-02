from __future__ import annotations

from orca_auto.flow.workflow import status as workflow_status


def test_workflow_status_helpers_cover_terminal_attention_and_current_stage_selection() -> None:
    assert workflow_status.normalize_workflow_status(None) == ""
    assert workflow_status.workflow_status_is_active("running") is True
    assert workflow_status.workflow_status_needs_attention("submission_failed") is True
    assert workflow_status.workflow_status_is_terminal("cancel_failed") is True
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
        [{"stage_id": "done", "status": "completed", "task_status": "completed"}]
    ) == {"stage_id": "done", "status": "completed", "task_status": "completed"}
