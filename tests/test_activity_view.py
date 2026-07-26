from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto import activity_view


def test_default_visible_items_hide_internal_workflow_children() -> None:
    items: list[dict[str, Any]] = [
        {"activity_id": "wf_1", "kind": "workflow"},
        {
            "activity_id": "crest_1",
            "kind": "job",
            "engine": "crest",
            "metadata": {"workflow_id": "wf_1"},
        },
        {
            "activity_id": "xtb_1",
            "kind": "job",
            "engine": "xtb",
            "metadata": {"workflow_id": "wf_1"},
        },
        {
            "activity_id": "orca_1",
            "kind": "job",
            "engine": "orca",
            "metadata": {"workflow_id": "wf_1"},
        },
        {"activity_id": "engine_job", "kind": "job", "engine": "xtb", "metadata": {}},
    ]

    visible = activity_view.queue_list_default_visible_items(items)

    assert [item["activity_id"] for item in visible] == ["wf_1", "orca_1", "engine_job"]
    assert visible[1]["parent_workflow_id"] == "wf_1"


def test_normalize_activity_filter_values_deduplicates_case_insensitively() -> None:
    assert activity_view.normalize_activity_filter_values([" ORCA ", "", "orca", "XTB"]) == (
        "orca",
        "xtb",
    )


def test_filter_activity_items_applies_normalized_engine_status_and_kind_filters() -> None:
    items: list[dict[str, Any]] = [
        {"activity_id": "orca_1", "engine": "ORCA", "status": " Running ", "kind": "job"},
        {"activity_id": "xtb_1", "engine": "xtb", "status": "running", "kind": "job"},
        {"activity_id": "wf_1", "engine": "workflow", "status": "running", "kind": "workflow"},
    ]

    filtered = activity_view.filter_activity_items(
        items,
        engines=["orca"],
        statuses=["running"],
        kinds=["job"],
    )

    assert [item["activity_id"] for item in filtered] == ["orca_1"]
    assert filtered[0] is not items[0]


def test_queue_list_display_rows_groups_children_under_workflow_once() -> None:
    workflow: dict[str, Any] = {
        "activity_id": "wf_1",
        "kind": "workflow",
        "metadata": {"template_name": "reaction_ts_search"},
    }
    visible_items: list[dict[str, Any]] = [
        {
            "activity_id": "orca_1",
            "kind": "job",
            "engine": "orca",
            "metadata": {"workflow_id": "wf_1"},
        },
        workflow,
        {
            "activity_id": "orca_2",
            "kind": "job",
            "engine": "orca",
            "metadata": {"workflow_id": "wf_1"},
        },
        {"activity_id": "engine_job", "kind": "job", "engine": "crest", "metadata": {}},
    ]

    rows = activity_view.queue_list_display_rows(
        all_items=[workflow],
        visible_items=visible_items,
        show_workflow_context=True,
        visible_workflow_child_engines=["orca"],
    )

    assert [(indent, item["activity_id"]) for indent, item in rows] == [
        (0, "wf_1"),
        (1, "orca_1"),
        (1, "orca_2"),
        (0, "engine_job"),
    ]


@pytest.mark.parametrize("stage_dirname", ["02_xtb", "03_orca"])
def test_activity_with_parent_hint_extracts_workflow_id_from_runtime_path(
    stage_dirname: str,
) -> None:
    item = {
        "activity_id": "xtb_child",
        "kind": "job",
        "metadata": {"job_dir": f"/tmp/root/wf_path/{stage_dirname}/job_1"},
    }

    assert activity_view.activity_with_parent_hint(item)["parent_workflow_id"] == "wf_path"


def test_count_global_active_simulations_uses_orca_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []
    admission_root = Path("/tmp/orca_auto-admission")

    def fake_engine_runtime_paths(
        config_path: str, *, engine: str | None = None
    ) -> dict[str, Path]:
        calls.append((config_path, engine))
        return {"admission_root": admission_root}

    monkeypatch.setattr(activity_view, "engine_runtime_paths", fake_engine_runtime_paths)
    monkeypatch.setattr(activity_view, "read_active_slot_count", lambda root: 5)

    assert (
        activity_view.count_global_active_simulations(
            [{"activity_id": "running_1"}], config_path="/tmp/orca_auto.yaml"
        )
        == 5
    )
    assert calls == [("/tmp/orca_auto.yaml", "orca")]


def test_activity_counter_config_path_prioritizes_sources_or_hints() -> None:
    payload = {
        "sources": {
            "orca_config": " ",
            "crest_config": " /tmp/crest.yaml ",
            "xtb_config": "/tmp/xtb.yaml",
        }
    }

    assert activity_view.activity_counter_config_path(payload) == "/tmp/crest.yaml"
    assert (
        activity_view.activity_counter_config_path(
            payload,
            config_hints=("/tmp/hint.yaml",),
            prefer_hints=True,
        )
        == "/tmp/hint.yaml"
    )
    assert (
        activity_view.activity_counter_config_path(
            {"sources": {}},
            config_hints=(None, "  ", "/tmp/fallback.yaml"),
        )
        == "/tmp/fallback.yaml"
    )
