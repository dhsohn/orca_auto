from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from orca_auto.orca.commands import run_inp_execution
from orca_auto.orca.state import load_state, save_state


def _write_running_state(reaction_dir: Path) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    save_state(
        reaction_dir,
        {
            "run_id": "run_active",
            "reaction_dir": str(reaction_dir),
            "selected_inp": str(inp),
            "max_retries": 2,
            "status": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "attempts": [],
            "final_result": None,
        },
    )


def _write_lock(reaction_dir: Path, *, pid: int = 4321, ticks: int = 111) -> None:
    (reaction_dir / "run.lock").write_text(
        json.dumps(
            {
                "pid": pid,
                "started_at": "2026-01-01T00:00:00+00:00",
                "process_start_ticks": ticks,
            }
        ),
        encoding="utf-8",
    )


def test_recover_crashed_state_skips_live_lock_with_matching_start_ticks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_running_state(reaction_dir)
    _write_lock(reaction_dir, ticks=111)
    monkeypatch.setattr(
        "orca_auto.core.utils.process_tracking.process_lock.is_process_alive",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
        lambda _pid: 111,
    )

    recovered = run_inp_execution.recover_crashed_state(
        reaction_dir,
        logger=logging.getLogger("test_recover_crashed_state"),
    )

    assert recovered is False
    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "running"


@pytest.mark.parametrize("observed_ticks", [222, None])
def test_recover_crashed_state_treats_reused_pid_lock_as_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_ticks: int | None,
) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_running_state(reaction_dir)
    _write_lock(reaction_dir, ticks=111)
    monkeypatch.setattr(
        "orca_auto.core.utils.process_tracking.process_lock.is_process_alive",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
        lambda _pid: observed_ticks,
    )

    recovered = run_inp_execution.recover_crashed_state(
        reaction_dir,
        logger=logging.getLogger("test_recover_crashed_state"),
    )

    assert recovered is True
    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "failed"
    assert state["final_result"] == {
        "status": "failed",
        "reason": "crashed_recovery",
        "analyzer_status": "incomplete",
    }


def _write_interrupted_state(reaction_dir: Path) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! Opt\n", encoding="utf-8")
    save_state(
        reaction_dir,
        {
            "run_id": "run_interrupted",
            "reaction_dir": str(reaction_dir),
            "selected_inp": str(inp),
            "max_retries": 2,
            "status": "failed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "attempts": [{"index": 1, "out_path": str(reaction_dir / "rxn.out")}],
            "final_result": {"status": "failed", "reason": "interrupted_by_user"},
        },
    )


def test_recover_crashed_state_reaps_orphan_for_interrupted_failed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A local Ctrl-C leaves status=failed/interrupted_by_user (which
    # load_or_create_state still resumes). The orphaned ORCA group must be
    # reaped even though this is not a running/retrying "crash", or the resumed
    # rerun races a live calculation over the same output.
    reaction_dir = tmp_path / "rxn"
    _write_interrupted_state(reaction_dir)
    reaped: list[Path] = []

    def _fake_reaper(reaction_dir: Path, *, logger: logging.Logger) -> bool:
        reaped.append(reaction_dir)
        return False

    monkeypatch.setattr(run_inp_execution, "recover_orphaned_orca_process", _fake_reaper)

    recovered = run_inp_execution.recover_crashed_state(
        reaction_dir,
        logger=logging.getLogger("test_recover_crashed_state"),
    )

    # The status transition itself is a no-op (not running/retrying)...
    assert recovered is False
    state = load_state(reaction_dir)
    assert state is not None and state["status"] == "failed"
    # ...but the orphan reaper ran regardless.
    assert reaped == [reaction_dir]


def test_recover_crashed_state_does_not_reap_under_a_live_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir = tmp_path / "rxn"
    _write_interrupted_state(reaction_dir)
    _write_lock(reaction_dir, ticks=111)
    monkeypatch.setattr(
        "orca_auto.core.utils.process_tracking.process_lock.is_process_alive",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        "orca_auto.core.utils.process_tracking.process_lock.process_start_ticks",
        lambda _pid: 111,
    )
    reaped: list[Path] = []

    def _fake_reaper(reaction_dir: Path, *, logger: logging.Logger) -> bool:
        reaped.append(reaction_dir)
        return False

    monkeypatch.setattr(run_inp_execution, "recover_orphaned_orca_process", _fake_reaper)

    recovered = run_inp_execution.recover_crashed_state(
        reaction_dir,
        logger=logging.getLogger("test_recover_crashed_state"),
    )

    # A live lock owner means another instance is active: touch nothing.
    assert recovered is False
    assert reaped == []
