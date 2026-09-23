from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto import activity_view


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
            "orca_config": " /tmp/orca.yaml ",
        }
    }

    assert activity_view.activity_counter_config_path(payload) == "/tmp/orca.yaml"
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
