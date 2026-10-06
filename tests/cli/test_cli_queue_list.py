"""Unified list command tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from orca_auto.cli import main
from orca_auto.core.admission import activate_reserved_slot, reserve_slot
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.machine_observation import report_json_path
from orca_auto.orca.queue.adapter import (
    enqueue,
    mark_completed,
    update_metadata,
)
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.state_reading import state_path
from tests.conftest import claim_next_entry, enqueue_entry, make_queue_entry, write_run_state
from tests.engine_artifact_helpers import orca_artifact_payload

MakeRun = Callable[..., None]


@pytest.fixture
def allowed(tmp_path: Path) -> Path:
    return tmp_path / "orca_runs"


@pytest.fixture
def config(allowed: Path, config_path: Callable[..., Path]) -> Path:
    return config_path(runs_root=allowed)


@pytest.fixture
def make_run(allowed: Path) -> Iterator[MakeRun]:
    """Persist a ``job_state.json`` and, unless ``queued=False``, its queue row.

    In-progress runs also hold their run lock.
    """

    with ExitStack() as stack:

        def factory(
            reaction_dir: Path,
            *,
            status: str = "completed",
            inp_name: str = "rxn.inp",
            run_id: str | None = None,
            queued: bool = True,
        ) -> None:
            run_id = run_id or f"run_{reaction_dir.name}"
            write_run_state(
                reaction_dir,
                status=status,
                run_id=run_id,
                selected_inp=reaction_dir / inp_name,
                attempts=[{"index": 1}],
            )
            if queued:
                running = status in {"running", "retrying"}
                enqueue_entry(
                    allowed,
                    make_queue_entry(
                        reaction_dir=reaction_dir,
                        status=QueueStatus.RUNNING if running else QueueStatus(status),
                        metadata={} if running else {"run_id": run_id},
                    ),
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


def _queue_row_detail(output: str, queue_id: str) -> str:
    """Detail column for one queue row (Status, Name, Detail, ID, Elapsed)."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] == queue_id:
            return parts[2]
    raise AssertionError(f"missing queue list row for {queue_id}")


# --- empty ----------------------------------------------------------------------------------


def test_list_empty(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert "active_simulations: 0" in output
    assert "- " not in output


# --- queued runs: each row borrows its own directory's run state ---------------------------


def test_shows_runs(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    make_run(allowed / "rxn1", status="completed")
    make_run(allowed / "rxn2", status="running")
    _activate_admission_slot(allowed, allowed / "rxn2")

    rc, output = _list(config, capsys=capsys)

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

    rc, output = _list(config, "--status", "running", capsys=capsys)

    assert rc == 0
    assert "rxn2" in output
    assert "rxn1" not in output
    assert "active_simulations: 1" in output


def test_nested_dirs(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    make_run(allowed / "project" / "rxn1", status="completed")
    make_run(allowed / "project" / "rxn2", status="failed")

    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert "rxn1" in output
    assert "rxn2" in output
    assert "active_simulations: 0" in output


def test_run_known_only_to_the_location_index_is_not_listed(
    tmp_path: Path,
    allowed: Path,
    config: Path,
    make_run: MakeRun,
    capsys: pytest.CaptureFixture[str],
) -> None:
    organized = tmp_path / "organized" / "project" / "rxn_tracked"
    make_run(
        organized, status="completed", inp_name="tracked.inp", run_id="run_tracked", queued=False
    )
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
    assert "run_tracked" not in output
    assert "✅" not in output
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
    assert _queue_row_detail(output, entry.queue_id) == "Unknown"
    assert _queue_row_detail(output, entry.queue_id) != "ORCA"
    assert "⏳" in output


def test_filter_pending(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    rxn_a = allowed / "mol_A"
    rxn_a.mkdir()
    entry = enqueue(allowed, str(rxn_a))
    # Also add a standalone completed run
    make_run(allowed / "rxn_done", status="completed")

    rc, output = _list(config, "--status", "pending", capsys=capsys)

    assert rc == 0
    assert entry.queue_id in output
    assert _queue_row_detail(output, entry.queue_id) == "Unknown"
    assert _queue_row_detail(output, entry.queue_id) != "ORCA"
    assert "rxn_done" not in output


def test_queue_with_run_state(
    allowed: Path, config: Path, make_run: MakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    """Queue entry enriched with run_state data."""
    rxn_dir = allowed / "mol_A"
    rxn_dir.mkdir()
    entry = enqueue(allowed, str(rxn_dir))
    # Create a run_state for the same directory
    make_run(rxn_dir, status="running", inp_name="opt.inp", queued=False)

    rc, output = _list(config, capsys=capsys)

    assert rc == 0
    assert entry.queue_id in output
    assert "active_simulations: 0" in output
    # Run-state ``opt.inp`` is not admission detail metadata; the name must not read as Opt.
    detail = _queue_row_detail(output, entry.queue_id)
    assert detail == "Unknown"
    assert detail not in {"ORCA", "Opt"}
    assert "⏳" in output


def test_queue_list_shows_sp_only_for_rows_with_recorded_single_point_evidence(
    allowed: Path,
    config: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Installed 10.1.0 rows carry job_type "other" and the full bound generation
    # path but no detail_kind; their names are no operation evidence.
    monkeypatch.setenv("COLUMNS", "400")
    method_only = "! wB97X-D3BJ def2-TZVP TightSCF\n"
    other: dict[str, str] = {"job_type": "other"}
    rows: dict[str, tuple[str, str, str, dict[str, str]]] = {
        "q-sp-1": ("MeOPh_OH_TSD_IRC_F_sp_continuous_01", "sp.inp", method_only, other),
        "q-sp-2": ("MeOPh_OH_TSD_IRC_F_sp_continuous_02", "sp.inp", method_only, other),
        "q-legacy-irc-content": ("MeOPh_TS_F_sp", "sp.inp", "! B3LYP def2-SVP IRC\n", other),
        "q-opt": ("MeOPh_OH_TSD_IRC_F_opt", "opt.inp", "! Opt\n", {"job_type": "opt"}),
        "q-irc": ("MeOPh_OH_TSD_IRC_F", "irc.inp", "! B3LYP def2-SVP IRC\n", other),
        "q-arbitrary": ("TS_IRC_arbitrary", "calc.inp", method_only, other),
        "q-new-sp": ("TS_IRC_new", "calc.inp", method_only, {**other, "detail_kind": "sp"}),
        "q-new-irc": (
            "sp_new",
            "sp.inp",
            "! B3LYP def2-SVP IRC\n",
            {**other, "detail_kind": "irc"},
        ),
        "q-new-freq": (
            "TS_IRC_freq",
            "sp.inp",
            "! HF STO-3G\n%freq AnFreq true end\n",
            {**other, "detail_kind": "unknown"},
        ),
    }
    for queue_id, (job_name, inp_name, text, metadata) in rows.items():
        generation = allowed / job_name / "20261001-000000-0123abcd"
        generation.mkdir(parents=True)
        (generation / inp_name).write_text(text + "* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
        enqueue_entry(
            allowed,
            make_queue_entry(
                queue_id=queue_id,
                reaction_dir=allowed / job_name,
                metadata={**metadata, "selected_inp": str(generation / inp_name)},
            ),
        )
    queue_file = allowed / "queue.json"
    before = queue_file.read_bytes()
    inputs_before = {path: path.read_bytes() for path in allowed.rglob("*.inp")}

    rc, output = _list(config, capsys=capsys)
    json_rc = main(["queue", "list", "--json", "--config", str(config)])
    payload = json.loads(capsys.readouterr().out)

    assert (rc, json_rc) == (0, 0)
    # Status, Name, Detail, ID, Elapsed; no cell holds a space here.
    details = {
        line.split()[3]: line.split()[2]
        for line in output.splitlines()
        if len(line.split()) >= 4 and line.split()[3] in rows
    }
    assert details == {
        "q-sp-1": "Unknown",
        "q-sp-2": "Unknown",
        "q-legacy-irc-content": "Unknown",
        "q-opt": "Opt",
        "q-irc": "Unknown",
        "q-arbitrary": "Unknown",
        "q-new-sp": "SP",
        "q-new-irc": "IRC",
        "q-new-freq": "Unknown",
    }
    # The JSON keeps the persisted coarse type and carries a detail kind only
    # where one was recorded; listing rewrites nothing.
    assert {row["activity_id"]: row["metadata"]["job_type"] for row in payload["activities"]} == {
        queue_id: metadata["job_type"] for queue_id, (_name, _inp, _text, metadata) in rows.items()
    }
    assert {
        row["activity_id"]: row["metadata"].get("detail_kind") for row in payload["activities"]
    } == {
        queue_id: metadata.get("detail_kind")
        for queue_id, (_name, _inp, _text, metadata) in rows.items()
    }
    assert queue_file.read_bytes() == before
    assert {path: path.read_bytes() for path in allowed.rglob("*.inp")} == inputs_before


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
    make_run(allowed / "rxn1", status="completed", queued=False)
    make_run(allowed / "rxn2", status="running", queued=False)

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
