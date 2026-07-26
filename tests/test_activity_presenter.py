from __future__ import annotations

from typing import Any

from orca_auto import activity_presenter


def _workflow_payload() -> dict[str, list[dict[str, Any]]]:
    return {
        "activities": [
            {"activity_id": "wf_1", "kind": "workflow", "status": "running"},
            {
                "activity_id": "crest_1",
                "kind": "job",
                "engine": "crest",
                "status": "completed",
                "metadata": {"workflow_id": "wf_1"},
            },
            {
                "activity_id": "xtb_1",
                "kind": "job",
                "engine": "xtb",
                "status": "running",
                "metadata": {"workflow_id": "wf_1"},
            },
            {
                "activity_id": "orca_1",
                "kind": "job",
                "engine": "orca",
                "status": "running",
                "metadata": {"workflow_id": "wf_1"},
            },
            {
                "activity_id": "standalone",
                "kind": "job",
                "engine": "crest",
                "status": "pending",
                "label": "standalone",
                "metadata": {},
            },
        ]
    }


def test_queue_list_display_rows_for_request_applies_default_visibility() -> None:
    rows = activity_presenter.queue_list_display_rows_for_request(
        _workflow_payload(),
        request=activity_presenter.QueueListPresentationRequest(
            default_visible_items=True,
        ),
    )

    assert [(indent, item["activity_id"]) for indent, item in rows] == [
        (0, "wf_1"),
        (1, "crest_1"),
        (1, "xtb_1"),
        (1, "orca_1"),
        (0, "standalone"),
    ]


def test_queue_list_text_presentation_reuses_display_row_request() -> None:
    presentation = activity_presenter.queue_list_text_presentation(
        _workflow_payload(),
        request=activity_presenter.QueueListPresentationRequest(
            default_visible_items=True,
            active_simulations=9,
            include_id=False,
        ),
    )

    assert presentation.active_simulations == 9
    assert [item["activity_id"] for _indent, item in presentation.display_rows] == [
        "wf_1",
        "crest_1",
        "xtb_1",
        "orca_1",
        "standalone",
    ]
    assert presentation.lines[0] == "active_simulations: 9"
    assert "ID" not in presentation.lines[1]
