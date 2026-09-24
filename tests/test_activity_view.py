from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto import activity_view


def test_normalize_activity_filter_values_deduplicates_case_insensitively() -> None:
    assert activity_view.normalize_activity_filter_values([" ORCA ", "", "orca", "XTB"]) == (
        "orca",
        "xtb",
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

    assert (
        activity_view.global_active_simulations(config_path="/tmp/orca_auto.yaml", fallback=1) == 5
    )
    assert calls == ["/tmp/orca_auto.yaml"]


def test_global_active_simulations_falls_back_to_the_listing_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert activity_view.global_active_simulations(config_path=None, fallback=3) == 3
    assert activity_view.global_active_simulations(config_path=" ", fallback=-1) == 0

    monkeypatch.setattr(activity_view, "engine_runtime_paths", lambda config_path: {})
    assert activity_view.global_active_simulations(config_path="/tmp/x.yaml", fallback=2) == 2

    monkeypatch.setattr(
        activity_view, "engine_runtime_paths", lambda config_path: {"admission_root": Path("/a")}
    )

    def unreadable(root: Path) -> int:
        raise OSError("admission store unavailable")

    monkeypatch.setattr(activity_view, "read_active_slot_count", unreadable)
    assert activity_view.global_active_simulations(config_path="/tmp/x.yaml", fallback=4) == 4
