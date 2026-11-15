"""Unit tests for the shared ``settlement.settle`` sequence (stages 1–2)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from orca_auto.orca.queue import settlement
from orca_auto.orca.queue.models import TerminalReplayWorkItem
from tests.conftest import make_app_cfg


def _item(queue_id: str = "q-1") -> TerminalReplayWorkItem:
    return TerminalReplayWorkItem(
        queue_root=Path("/q"),
        queue_id=queue_id,
        reaction_dir="/runs/job",
        reaction_key="job",
        task_id="task-1",
        observed_status="completed",
        selected_inp="h2.inp",
        error="",
    )


def test_settle_calls_steps_in_order() -> None:
    cfg = make_app_cfg(Path("/runs"))
    item = _item()
    steps: list[str] = []

    def track_row(name: str):
        def _inner(_item: TerminalReplayWorkItem) -> TerminalReplayWorkItem:
            steps.append(name)
            return _item

        return _inner

    def track_finish(_cfg: object, _item: TerminalReplayWorkItem) -> None:
        steps.append("finish")

    with (
        patch.object(settlement, "prepare", side_effect=track_row("prepare")),
        patch.object(settlement, "bind_row", side_effect=track_row("bind")),
        patch.object(settlement, "finish", side_effect=track_finish),
    ):
        settlement.settle(
            cfg,
            item,
            has_row=True,
            retain=lambda _it: steps.append("retain"),
            release=lambda _it: steps.append("release"),
        )

    assert steps == ["prepare", "retain", "bind", "retain", "release", "finish"]


def test_settle_finish_failure_raises_publication_pending_with_cause() -> None:
    cfg = make_app_cfg(Path("/runs"))
    item = _item()
    root_exc = RuntimeError("publish failed")

    with (
        patch.object(settlement, "prepare", return_value=item),
        patch.object(settlement, "bind_row", return_value=item),
        patch.object(settlement, "finish", side_effect=root_exc),
    ):
        with pytest.raises(settlement.PublicationPending) as pending:
            settlement.settle(cfg, item, has_row=True, retain=lambda _it: None)
    assert pending.value.item is item
    assert pending.value.__cause__ is root_exc


def test_settle_release_failure_propagates_unwrapped() -> None:
    cfg = make_app_cfg(Path("/runs"))
    item = _item()
    release_exc = OSError("slot write failed")

    with (
        patch.object(settlement, "prepare", return_value=item),
        patch.object(settlement, "bind_row", return_value=item),
        patch.object(settlement, "finish") as finish,
    ):
        with pytest.raises(OSError, match="slot write failed") as caught:
            settlement.settle(
                cfg,
                item,
                has_row=True,
                retain=lambda _it: None,
                release=lambda _it: (_ for _ in ()).throw(release_exc),
            )
    assert caught.value is release_exc
    finish.assert_not_called()


def test_settle_without_row_skips_bind_and_release() -> None:
    cfg = make_app_cfg(Path("/runs"))
    item = _item()
    calls: list[str] = []

    def track_publish(_cfg: object, _item: TerminalReplayWorkItem) -> None:
        calls.append("publish")

    with (
        patch.object(settlement, "prepare", return_value=item),
        patch.object(settlement, "bind_row", side_effect=AssertionError("bind_row")),
        patch.object(settlement, "_publish", side_effect=track_publish),
        patch.object(settlement, "finish", side_effect=AssertionError("finish")),
    ):
        settlement.settle(cfg, item, has_row=False, retain=lambda _it: None)

    assert calls == ["publish"]


@pytest.mark.skipif(sys.platform == "darwin", reason="admission owner identity needs Linux boot_id")
def test_live_owner_keeps_item_on_job_until_release_succeeds(
    worker,
    fake_children,
    queue_root: Path,
) -> None:
    from orca_auto.core.admission import admission_dir
    from orca_auto.orca.queue.adapter import enqueue
    from orca_auto.orca.statuses import RunStatus
    from tests.conftest import claim_next_entry, write_run_state
    from tests.queue_worker_helpers import reserve_job_slot, running_job

    rxn = queue_root / "single_owner"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-single-owner")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    worker._running[entry.queue_id] = job
    replay = worker.replay_state.pending_replays

    with patch.object(worker, "_release_admission_slot", side_effect=OSError("slot write failed")):
        worker._check_completed_jobs()

    assert worker._running[entry.queue_id] is job
    assert job.finalization is not None and job.finalization.item is not None
    assert entry.queue_id not in replay

    with patch.object(settlement, "finish", side_effect=RuntimeError("publish failed")):
        worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert job.finalization is None
    assert entry.queue_id in replay
