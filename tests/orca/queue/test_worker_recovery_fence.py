"""Per-job recovery fence: unrelated queue rows advance while tracked jobs finalize."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from orca_auto.core.admission import (
    admission_dir,
    get_slot,
    list_slots,
    prepare_slot_engine_process,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.queue import replay, roots, settlement
from orca_auto.orca.queue.adapter import enqueue
from orca_auto.orca.queue.models import OrcaWorkerReplayState
from orca_auto.orca.queue.terminal_marker import terminal_replay_marker_from_entry
from orca_auto.orca.statuses import RunStatus
from tests.conftest import RecordingChannel, claim_next_entry, make_app_cfg, write_run_state
from tests.queue_worker_helpers import (
    FakeChildren,
    job_record,
    queue_row,
    queue_statuses,
    reserve_job_slot,
    running_job,
)


def test_fenced_reconcile_pass_keeps_running_cursor(queue_root: Path) -> None:
    cfg = make_app_cfg(queue_root)
    rxn = queue_root / "fenced_rxn"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    reaction_key = settlement.reaction_key_for_dir(str(rxn.resolve()))
    assert reaction_key
    replay_state = OrcaWorkerReplayState(
        reconcile_statuses={entry.queue_id: "running"},
        generation_owners={reaction_key: entry.queue_id},
        generation_owner_active={reaction_key: True},
    )
    before_rows = roots.list_orca_rows(cfg)

    replay.reconcile_terminal_replays(
        cfg,
        replay_state,
        before_rows,
        fenced_reaction_keys=frozenset({reaction_key}),
    )

    assert replay_state.reconcile_statuses is not None
    assert replay_state.reconcile_statuses[entry.queue_id] == "running"
    assert replay_state.generation_owners[reaction_key] == entry.queue_id
    assert replay_state.generation_owner_active[reaction_key] is True


@pytest.mark.skipif(sys.platform == "darwin", reason="admission owner identity needs Linux boot_id")
def test_recovery_pass_advances_unrelated_row_while_engine_recovery_retains_job(
    worker,
    fake_children: FakeChildren,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    retained_rxn = queue_root / "retained_fence"
    retained_rxn.mkdir()
    orphan_rxn = queue_root / "orphan_fence"
    orphan_rxn.mkdir()
    retained = enqueue(queue_root, str(retained_rxn), task_id="task-retained")
    orphan = enqueue(queue_root, str(orphan_rxn), task_id="task-orphan")
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    write_run_state(orphan_rxn, status=RunStatus.RUNNING, job_id=orphan.task_id)
    owner = sleeping_child()
    token = reserve_job_slot(
        admission_dir(queue_root),
        worker.max_concurrent,
        retained,
        retained_rxn,
        owner_pid=owner.pid,
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(admission_dir(queue_root), token) is not None
    worker._running[retained.queue_id] = running_job(
        worker, retained, retained_rxn, fake_children.spawn(exited=1), token
    )
    worker._worker_state_last_reconcile = None

    with patch.object(worker, "_reconcile_snapshot_intents_if_due"):
        worker._reconcile_worker_state_now()

    assert retained.queue_id in worker._running
    assert len(list_slots(admission_dir(queue_root))) == 1
    assert queue_statuses(queue_root)[retained.queue_id] == QueueStatus.RUNNING
    assert queue_statuses(queue_root)[orphan.queue_id] != QueueStatus.RUNNING

    owner.kill()
    owner.wait()


def test_fail_closed_fence_leaves_reconcile_clock_unadvanced(
    worker,
    fake_children: FakeChildren,
    queue_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rxn = queue_root / "fail_closed_fence"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    job = running_job(worker, entry, rxn, fake_children.spawn(), "fence_slot")
    job.reaction_dir = ""
    worker._running[entry.queue_id] = job
    worker._worker_state_last_reconcile = None

    with caplog.at_level(logging.WARNING):
        worker._reconcile_worker_state_now()
        worker._reconcile_worker_state_now()

    assert worker._worker_state_last_reconcile is None
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert (
        warnings.count(
            "ORCA worker recovery pass skipped (fail closed): tracked job reaction "
            f"directory cannot be resolved safely: queue_id={entry.queue_id} "
            "reason=blank_reaction_dir"
        )
        == 1
    )

    job.reaction_dir = str(rxn)
    with caplog.at_level(logging.WARNING):
        worker._reconcile_worker_state_now()
        job.reaction_dir = ""
        worker._reconcile_worker_state_now()

    assert worker._worker_state_last_reconcile is not None
    msg = (
        "ORCA worker recovery pass skipped (fail closed): tracked job reaction "
        f"directory cannot be resolved safely: queue_id={entry.queue_id} "
        "reason=blank_reaction_dir"
    )
    assert sum(1 for record in caplog.records if record.getMessage() == msg) == 2


def test_fenced_blocked_marker_keys_preserved_when_unfenced_replay_raises(
    queue_root: Path,
) -> None:
    cfg = make_app_cfg(queue_root)
    rxn = queue_root / "fenced_blocked_marker"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    reaction_key = settlement.reaction_key_for_dir(str(rxn.resolve()))
    assert reaction_key
    replay_state = OrcaWorkerReplayState(blocked_marker_keys={entry.queue_id})
    before_rows = roots.list_orca_rows(cfg)

    with patch.object(
        replay,
        "_replay_current_terminal_entries",
        side_effect=RuntimeError("unfenced replay failed"),
    ):
        with pytest.raises(RuntimeError, match="unfenced replay failed"):
            replay.reconcile_terminal_replays(
                cfg,
                replay_state,
                before_rows,
                fenced_reaction_keys=frozenset({reaction_key}),
            )

    assert entry.queue_id in replay_state.blocked_marker_keys


@pytest.mark.skipif(sys.platform == "darwin", reason="admission owner identity needs Linux boot_id")
def test_fenced_recovery_pass_advances_orphan_while_live_release_failure_retains_job(
    worker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    retained_rxn = queue_root / "retained_release_fail"
    retained_rxn.mkdir()
    orphan_rxn = queue_root / "orphan_release_fail"
    orphan_rxn.mkdir()
    retained = enqueue(queue_root, str(retained_rxn), task_id="task-retained-release")
    orphan = enqueue(queue_root, str(orphan_rxn), task_id="task-orphan-release")
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    write_run_state(
        retained_rxn,
        status=RunStatus.COMPLETED,
        job_id=retained.task_id,
    )
    write_run_state(orphan_rxn, status=RunStatus.RUNNING, job_id=orphan.task_id)
    token = reserve_job_slot(
        admission_dir(queue_root),
        worker.max_concurrent,
        retained,
        retained_rxn,
    )
    job = running_job(worker, retained, retained_rxn, fake_children.spawn(exited=0), token)
    worker._running[retained.queue_id] = job
    release_calls: list[str] = []

    def fail_release(admission_token: str) -> object:
        release_calls.append(admission_token)
        raise OSError("slot write failed")

    with patch.object(worker, "_release_admission_slot", side_effect=fail_release):
        worker._check_completed_jobs()

    assert release_calls == [token]
    assert retained.queue_id in worker._running
    assert job.finalization is not None
    assert job.finalization.item is not None
    assert job.finalization.item.state_prepared
    assert job.finalization.item.queue_id not in worker.replay_state.pending_replays
    assert get_slot(admission_dir(queue_root), token) is not None
    assert terminal_replay_marker_from_entry(queue_row(queue_root, retained.queue_id))
    assert job_record(queue_root, retained.task_id) is None
    assert recording_channel.sends == []

    worker._worker_state_last_reconcile = None
    with patch.object(worker, "_reconcile_snapshot_intents_if_due"):
        worker._reconcile_worker_state_now()

    assert retained.queue_id in worker._running
    assert job.finalization is not None and job.finalization.item is not None
    assert get_slot(admission_dir(queue_root), token) is not None
    assert terminal_replay_marker_from_entry(queue_row(queue_root, retained.queue_id))
    assert queue_statuses(queue_root)[retained.queue_id] == QueueStatus.COMPLETED
    assert queue_statuses(queue_root)[orphan.queue_id] != QueueStatus.RUNNING
    assert recording_channel.sends == []

    worker._check_completed_jobs()
    assert retained.queue_id not in worker._running
    assert get_slot(admission_dir(queue_root), token) is None
    assert job.finalization is None
    assert worker.replay_state.pending_replays == {}
    assert queue_row(queue_root, retained.queue_id).metadata.get("orca_terminal_replay") is None
    assert queue_statuses(queue_root)[retained.queue_id] == QueueStatus.COMPLETED
    assert job_record(queue_root, retained.task_id) is not None
    assert len(recording_channel.sends) == 1
