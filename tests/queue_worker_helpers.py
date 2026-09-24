from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Protocol
from unittest.mock import patch

from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca.attempt.reporting import build_final_result
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue.models import OrcaWorkerReplayState
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.submission import create_queued_submission
from tests.conftest import make_app_cfg, write_config_file, write_fake_orca, write_run_state


class ReplayStateOwner(Protocol):
    """What ``replay.reconcile_worker_state`` needs: an ``OrcaQueueWorker`` or a stand-in."""

    @property
    def cfg(self) -> AppConfig: ...

    @property
    def admission_root(self) -> str | Path: ...

    @property
    def replay_state(self) -> OrcaWorkerReplayState: ...


def queued_submission(tmp_path: Path) -> tuple[AppConfig, Path, QueueEntry, Path]:
    """A real config file, loaded config, one queued job and its fake ORCA executable."""

    runs_root = tmp_path / "runs"
    job_dir = runs_root / "job"
    job_dir.mkdir(parents=True)
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    executable = write_fake_orca(tmp_path / "fake-orca")
    config_path = write_config_file(
        tmp_path / "orca_auto.yaml",
        make_app_cfg(runs_root, orca_executable=executable, max_concurrent=1),
    )
    cfg = load_config(str(config_path))
    queued = create_queued_submission(
        cfg, Namespace(force=False, priority=10), job_dir, selected_inp=selected
    ).entry
    return cfg, config_path, queued, executable


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
    out_path = str(reaction_dir / "rxn.out")
    write_run_state(
        reaction_dir,
        status=RunStatus.COMPLETED,
        job_id="task_terminal_123",
        selected_inp=selected_inp,
        attempts=[
            {
                "index": 1,
                "inp_path": str(selected_inp),
                "out_path": out_path,
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-05-29T12:00:00+00:00",
                "ended_at": "2026-05-29T12:01:00+00:00",
            }
        ],
        final_result=build_final_result(
            status=RunStatus.COMPLETED,
            analyzer_status=AnalyzerStatus.COMPLETED,
            reason="normal_termination",
            last_out_path=out_path,
            resumed=False,
        ),
    )


def reconcile_statuses(worker: ReplayStateOwner) -> dict[tuple[str, str], str]:
    statuses = worker.replay_state.reconcile_statuses
    assert statuses is not None
    return statuses


def run_terminal_replay(
    worker: ReplayStateOwner,
    tmp_path: Path,
    entry: QueueEntry,
    *,
    previous_status: str | None = None,
) -> None:
    if previous_status is not None:
        state = worker.replay_state
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
        patch.object(replay_mod, "reconcile_stale_slots"),
        patch.object(replay_mod, "reconcile_orphaned_running_entries"),
    ):
        replay_mod.reconcile_worker_state(
            worker.cfg,
            admission_root=worker.admission_root,
            replay_state=worker.replay_state,
        )


__all__ = [
    "ReplayStateOwner",
    "current_orca_queue_metadata",
    "queued_submission",
    "reconcile_statuses",
    "run_terminal_replay",
    "write_completed_run_state",
]
