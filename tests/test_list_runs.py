"""Unified list command tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from orca_auto.cli import main
from orca_auto.core.admission import activate_reserved_slot, reserve_slot
from orca_auto.orca.queue.adapter import (
    enqueue,
    mark_completed,
    update_metadata,
)
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.state_reading import report_json_path, state_path
from tests.conftest import claim_next_entry, write_run_state
from tests.engine_artifact_helpers import orca_artifact_payload

MakeRun = Callable[..., None]


@pytest.fixture
def allowed(tmp_path: Path) -> Path:
    return tmp_path / "orca_runs"


@pytest.fixture
def config(allowed: Path, config_path: Callable[..., Path]) -> Path:
    return config_path(runs_root=allowed)


@pytest.fixture
def make_run() -> Iterator[MakeRun]:
    """Persist a ``job_state.json``; in-progress runs also hold their run lock."""

    with ExitStack() as stack:

        def factory(
            reaction_dir: Path,
            *,
            status: str = "completed",
            inp_name: str = "rxn.inp",
            run_id: str | None = None,
        ) -> None:
            write_run_state(
                reaction_dir,
                status=status,
                run_id=run_id or f"run_{reaction_dir.name}",
                selected_inp=reaction_dir / inp_name,
                attempts=[{"index": 1}],
            )
            if status in {"running", "retrying"}:
                # A genuinely in-progress run holds a live run lock; without it the
                # activity list now treats the run as a stale/failed leftover.
                stack.enter_context(acquire_run_lock(reaction_dir))

        yield factory


def _activate_admission_slot(allowed_root: Path, reaction_dir: Path) -> None:
    """Mirror a live run: an active slot in <runs root>/.admission."""
    admission_root = allowed_root / ".admission"
    token = reserve_slot(
        admission_root,
        4,
        work_dir=str(reaction_dir),
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    activate_reserved_slot(admission_root, token)


def _list(config: Path, *extra: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = main(["queue", "list", "--config", str(config), *extra])
    return rc, capsys.readouterr().out


# --- empty ----------------------------------------------------------------------------------


def test_list_empty(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert "active_simulations: 0" in output
    assert "- " not in output


# --- standalone runs: unindexed runs are discovered explicitly, indexed ones listed normally


def test_shows_runs(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    make_run(allowed / "rxn1", status="completed")
    make_run(allowed / "rxn2", status="running")
    _activate_admission_slot(allowed, allowed / "rxn2")

    rc, output = _list(config, "--refresh", capsys=capsys)

    assert rc == 0
    assert "rxn1" in output
    assert "rxn2" in output
    assert "✅" in output
    assert "▶" in output
    assert "active_simulations: 1" in output


def test_filter(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    make_run(allowed / "rxn1", status="completed")
    make_run(allowed / "rxn2", status="running")
    _activate_admission_slot(allowed, allowed / "rxn2")

    rc, output = _list(config, "--status", "running", "--refresh", capsys=capsys)

    assert rc == 0
    assert "rxn2" in output
    assert "rxn1" not in output
    assert "active_simulations: 1" in output


def test_nested_dirs(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    make_run(allowed / "project" / "rxn1", status="completed")
    make_run(allowed / "project" / "rxn2", status="failed")

    rc, output = _list(config, "--refresh", capsys=capsys)

    assert rc == 0
    assert "rxn1" in output
    assert "rxn2" in output
    assert "active_simulations: 0" in output


def test_tracked_organized_run_is_listed_via_job_locations_index(
    tmp_path: Path,
    allowed: Path,
    config: Path,
    make_run: MakeRun,
    capsys: pytest.CaptureFixture[str],
) -> None:
    organized = tmp_path / "organized" / "project" / "rxn_tracked"
    make_run(organized, status="completed", inp_name="tracked.inp", run_id="run_tracked")
    (allowed / "job_locations.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "job_tracked",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(allowed / "project" / "rxn_tracked"),
                    "molecule_key": "rxn_tracked",
                    "selected_input_xyz": str(organized / "tracked.inp"),
                    "latest_known_path": str(organized),
                    "resource_request": {},
                    "resource_actual": {},
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert "run_tracked" in output
    assert "✅" in output
    assert "active_simulations: 0" in output


# --- queue entries in the unified view ------------------------------------------------------


def test_queue_entries_shown(
    allowed: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rxn_dir = allowed / "mol_A"
    rxn_dir.mkdir()
    entry = enqueue(allowed, str(rxn_dir))

    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert "active_simulations: 0" in output
    assert entry.queue_id in output
    assert "ORCA" in output
    assert "⏳" in output


def test_filter_pending(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    rxn_a = allowed / "mol_A"
    rxn_a.mkdir()
    entry = enqueue(allowed, str(rxn_a))
    # Also add a standalone completed run
    make_run(allowed / "rxn_done", status="completed")

    rc, output = _list(config, "--status", "pending", "--refresh", capsys=capsys)

    assert rc == 0
    assert entry.queue_id in output
    assert "ORCA" in output
    assert "rxn_done" not in output


def test_queue_with_run_state(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    """Queue entry enriched with run_state data."""
    rxn_dir = allowed / "mol_A"
    rxn_dir.mkdir()
    entry = enqueue(allowed, str(rxn_dir))
    # Create a run_state for the same directory
    make_run(rxn_dir, status="running", inp_name="opt.inp")

    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert entry.queue_id in output
    assert "active_simulations: 0" in output
    assert "ORCA" in output
    assert "⏳" in output


def test_list_does_not_terminalize_orphaned_entry_from_root_report(
    allowed: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rxn_dir = allowed / "mol_done"
    rxn_dir.mkdir()
    entry = enqueue(allowed, str(rxn_dir))
    claim_next_entry(allowed)
    report_json_path(rxn_dir).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=entry.task_id,
                run_id="run_done_1",
                reaction_dir=str(rxn_dir),
                status="completed",
                final_result={
                    "status": "completed",
                    "completed_at": "2026-03-10T04:59:59+00:00",
                },
            )
        ),
        encoding="utf-8",
    )

    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert entry.queue_id in output
    assert "⏳" in output
    assert "✅" not in output
    assert "▶" not in output


# --- clear ----------------------------------------------------------------------------------


def test_clear_queue_terminal(
    allowed: Path, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rxn_dir = allowed / "mol_A"
    rxn_dir.mkdir()
    entry = enqueue(allowed, str(rxn_dir))
    mark_completed(allowed, entry.queue_id)
    # Mirror the worker after terminal side effects are durably published.
    assert update_metadata(allowed, entry.queue_id, {"orca_terminal_replay": None})

    rc, output = _list(config, "clear", capsys=capsys)

    assert rc == 0
    assert "Cleared" in output


def test_clear_standalone_terminal(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    make_run(allowed / "rxn1", status="completed")
    make_run(allowed / "rxn2", status="running")

    rc, _output = _list(config, "clear", capsys=capsys)

    assert rc == 0
    # rxn1 (completed) should be cleared
    assert not state_path(allowed / "rxn1").exists()
    # rxn2 (running) should remain
    assert state_path(allowed / "rxn2").exists()


def test_clear_empty(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, output = _list(config, "clear", capsys=capsys)

    assert rc == 0
    assert "Nothing to clear." in output
