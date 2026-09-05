from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca.attempt.reporting import build_final_result
from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.state import finalize_state, new_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus


def make_queue_worker_cfg(tmp: str) -> AppConfig:
    return AppConfig(runtime=OrcaRuntimeConfig(allowed_root=tmp))


def current_orca_queue_metadata(
    reaction_dir: Path,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    status = reaction_dir.stat()
    return {
        "reaction_dir": str(reaction_dir),
        "execution_snapshot": {
            "job_dir_identity": {
                "device": int(status.st_dev),
                "inode": int(status.st_ino),
            }
        },
        **(extra or {}),
    }


def write_completed_run_state(reaction_dir: Path) -> None:
    selected_inp = reaction_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    state = new_state(reaction_dir, selected_inp)
    state["job_id"] = "task_terminal_123"
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(reaction_dir / "rxn.out"),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-05-29T12:00:00+00:00",
            "ended_at": "2026-05-29T12:01:00+00:00",
        }
    )
    finalize_state(
        reaction_dir,
        state,
        status=RunStatus.COMPLETED.value,
        final_result=build_final_result(
            status=RunStatus.COMPLETED,
            analyzer_status=AnalyzerStatus.COMPLETED,
            reason="normal_termination",
            last_out_path=str(reaction_dir / "rxn.out"),
            resumed=False,
        ),
    )


def reconcile_statuses(worker: object) -> dict[tuple[str, str], str]:
    statuses = replay_mod.get_replay_state(worker).reconcile_statuses
    assert statuses is not None
    return statuses


def run_terminal_replay(
    worker: object,
    tmp_path: Path,
    entry: QueueEntry,
    *,
    previous_status: str | None = None,
) -> None:
    if previous_status is not None:
        state = replay_mod.get_replay_state(worker)
        statuses = dict(state.reconcile_statuses or {})
        statuses[(str(tmp_path.resolve()), entry.queue_id)] = previous_status
        state.reconcile_statuses = statuses
    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            return_value=[(tmp_path, entry)],
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
    ):
        replay_mod.reconcile_worker_state(worker)


__all__ = [
    "current_orca_queue_metadata",
    "make_queue_worker_cfg",
    "reconcile_statuses",
    "run_terminal_replay",
    "write_completed_run_state",
]
