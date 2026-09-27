from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto import activity_view


def test_normalize_activity_filter_values_deduplicates_case_insensitively() -> None:
    assert activity_view.normalize_activity_filter_values([" ORCA ", "", "orca", "OTHER"]) == (
        "orca",
        "other",
    )


def test_global_active_simulations_prefers_the_admission_slot_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    admission_root = Path("/tmp/orca_auto-admission")

    def fake_engine_runtime_paths(config_path: str) -> dict[str, Path]:
        calls.append(config_path)
        return {"admission_root": admission_root}

    monkeypatch.setattr(activity_view, "engine_runtime_paths", fake_engine_runtime_paths)
    monkeypatch.setattr(activity_view, "read_active_slot_count", lambda root: 5)

    assert activity_view.global_active_simulations(
        config_path="/tmp/orca_auto.yaml", fallback=1
    ) == (5, None)
    assert calls == ["/tmp/orca_auto.yaml"]


def test_global_active_simulations_falls_back_to_the_listing_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert activity_view.global_active_simulations(config_path=None, fallback=3) == (3, None)
    assert activity_view.global_active_simulations(config_path=" ", fallback=-1) == (0, None)

    monkeypatch.setattr(activity_view, "engine_runtime_paths", lambda config_path: {})
    assert activity_view.global_active_simulations(config_path="/tmp/x.yaml", fallback=2) == (
        2,
        None,
    )

    monkeypatch.setattr(
        activity_view, "engine_runtime_paths", lambda config_path: {"admission_root": Path("/a")}
    )

    def unreadable(root: Path) -> int:
        raise OSError("admission store unavailable")

    monkeypatch.setattr(activity_view, "read_active_slot_count", unreadable)
    assert activity_view.global_active_simulations(config_path="/tmp/x.yaml", fallback=4) == (
        4,
        None,
    )


def test_global_active_simulations_reports_a_corrupt_admission_store_as_a_blocker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    admission_root = root / ".admission"
    admission_root.mkdir(parents=True)
    slots_file = admission_root / "admission_slots.json"
    slots_file.write_text("{not json", encoding="utf-8")
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {root}\n", encoding="utf-8")

    count, blocker = activity_view.global_active_simulations(config_path=str(config), fallback=2)

    assert count == 2
    assert blocker is not None
    assert blocker["scope"] == "admission_store"
    assert blocker["queue_id"] == "*"
    assert blocker["allowed_root"] == str(root)
    assert str(slots_file) in blocker["reason"]
    assert blocker["next_action"]
