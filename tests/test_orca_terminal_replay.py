"""Tests for durable ORCA terminal replay and state reconciliation."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orca_auto.core.config import DiscordConfig, MessengerConfig
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from orca_auto.orca.config import AppConfig, RetryRuntimeConfig
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue import worker_tracking as worker_tracking_mod
from orca_auto.orca.queue.adapter import (
    cancel,
    dequeue_next,
    enqueue,
    list_queue,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.orca.queue.replay import (
    record_cancelled_run_state as _record_cancelled_run_state,
)
from orca_auto.orca.queue.replay import (
    record_failed_run_state as _record_failed_run_state,
)
from orca_auto.orca.queue.terminal_replay import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    terminal_replay_marker,
)
from orca_auto.orca.queue.worker_tracking import (
    get_run_id_from_state as _get_run_id_from_state,
)
from orca_auto.orca.state import (
    finalize_state,
    load_state,
    new_state,
    report_json_path,
    save_state,
    state_path,
)
from tests.engine_artifact_helpers import orca_artifact_payload
from tests.queue_worker_helpers import reconcile_statuses as _reconcile_statuses
from tests.queue_worker_helpers import run_terminal_replay as _run_terminal_replay


def _terminal_replay_entry(tmp_path: Path, status: QueueStatus) -> QueueEntry:
    return QueueEntry(
        queue_id="queue-replay",
        app_name="orca_auto_orca",
        task_id="task-replay",
        task_kind="orca_run_inp",
        engine="orca",
        status=status,
        metadata={"reaction_dir": str(tmp_path / "rxn")},
    )


class TestGetRunIdFromState(unittest.TestCase):
    def test_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _get_run_id_from_state(tmp)
            self.assertIsNone(result)

    def test_with_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_state(
                Path(tmp),
                {
                    "run_id": "test_run_123",
                    "reaction_dir": str(tmp),
                    "selected_inp": "",
                    "max_retries": 0,
                    "status": "completed",
                    "attempts": [],
                    "final_result": {},
                },
            )
            result = _get_run_id_from_state(tmp)
            self.assertEqual(result, "test_run_123")

    def test_expected_job_id_rejects_previous_generation_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_state(
                Path(tmp),
                {
                    "job_id": "task-a",
                    "run_id": "run-a",
                    "reaction_dir": str(tmp),
                    "selected_inp": "",
                    "max_retries": 0,
                    "status": "completed",
                    "attempts": [],
                    "final_result": {},
                },
            )

            self.assertIsNone(_get_run_id_from_state(tmp, expected_job_id="task-b"))
            self.assertEqual(
                _get_run_id_from_state(tmp, expected_job_id="task-a"),
                "run-a",
            )


@pytest.mark.parametrize(
    "terminal_status",
    [QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED],
)
@pytest.mark.parametrize("existing_cursor", [False, True])
@pytest.mark.parametrize("replay_marker", [None, {"version": 2}])
def test_worker_does_not_replay_unobserved_terminal_entry_without_valid_marker(
    tmp_path: Path,
    terminal_status: QueueStatus,
    existing_cursor: bool,
    replay_marker: object,
) -> None:
    entry = replace(
        _terminal_replay_entry(tmp_path, terminal_status),
        metadata={
            "reaction_dir": str(tmp_path / "rxn"),
            "run_id": "run-original",
            "orca_terminal_replay": replay_marker,
        },
    )
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    if existing_cursor:
        replay_mod.get_replay_state(worker).reconcile_statuses = {
            (str(tmp_path.resolve()), "other-queue"): STATUS_RUNNING
        }

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
        patch.object(
            replay_mod,
            "record_failed_run_state",
            return_value=("run-rewritten", STATUS_FAILED),
        ) as record_failed,
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            return_value=("run-rewritten", STATUS_CANCELLED),
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=True) as update,
        patch.object(
            worker_tracking_mod,
            "upsert_terminal_job_record",
            return_value=True,
        ) as upsert,
        patch.object(
            worker_tracking_mod,
            "notify_terminal_job_from_state",
            return_value=False,
        ) as notify,
        patch.object(replay_mod, "_clear_terminal_replay_marker") as clear_marker,
    ):
        replay_mod.reconcile_worker_state(worker)

    record_failed.assert_not_called()
    record_cancelled.assert_not_called()
    update.assert_not_called()
    upsert.assert_not_called()
    notify.assert_not_called()
    clear_marker.assert_not_called()
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == terminal_status.value
    assert replay_mod.get_replay_state(worker).pending_replays == {}


def test_repeated_worker_startup_preserves_historical_failed_queue_bytes(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    entry = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.FAILED),
        finished_at="2026-07-14T10:51:04+00:00",
        error="retry_limit_reached",
        metadata={
            "reaction_dir": str(reaction_dir),
            "run_id": "run-original",
            "orca_terminal_replay": None,
        },
    )
    save_entries_core(tmp_path, [entry])
    queue_file = tmp_path / "queue.json"
    queue_bytes = queue_file.read_bytes()
    queue_mtime_ns = queue_file.stat().st_mtime_ns
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))

    def current_entries(_cfg: AppConfig) -> list[tuple[Path, QueueEntry]]:
        return [(tmp_path, list_queue(tmp_path)[0])]

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            side_effect=current_entries,
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            replay_mod,
            "record_failed_run_state",
            wraps=replay_mod.record_failed_run_state,
        ) as record_failed,
        patch.object(
            replay_mod,
            "update_terminal",
            wraps=replay_mod.update_terminal,
        ) as update,
        patch.object(
            worker_tracking_mod,
            "upsert_terminal_job_record",
            return_value=True,
        ) as upsert,
        patch.object(
            worker_tracking_mod,
            "notify_terminal_job_from_state",
            return_value=False,
        ) as notify,
    ):
        for _restart in range(2):
            worker = MagicMock(cfg=cfg, admission_root=tmp_path)
            replay_mod.reconcile_worker_state(worker)

    record_failed.assert_not_called()
    update.assert_not_called()
    upsert.assert_not_called()
    notify.assert_not_called()
    assert queue_file.read_bytes() == queue_bytes
    assert queue_file.stat().st_mtime_ns == queue_mtime_ns
    assert not state_path(reaction_dir).exists()
    assert not report_json_path(reaction_dir).exists()
    [preserved] = list_queue(tmp_path)
    assert preserved.finished_at == entry.finished_at
    assert preserved.error == entry.error
    assert preserved.metadata["run_id"] == "run-original"


@pytest.mark.parametrize(
    ("writer", "expected_status"),
    [
        ("pending_cancel", STATUS_CANCELLED),
        ("start_like_failure", STATUS_FAILED),
        ("requeue_cancel", STATUS_CANCELLED),
        ("orphan_cancel", STATUS_CANCELLED),
    ],
)
def test_terminal_writer_marker_replays_once_after_fresh_worker_restart(
    tmp_path: Path,
    writer: str,
    expected_status: str,
) -> None:
    reaction_dir = tmp_path / writer
    reaction_dir.mkdir()
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id=f"task-{writer}",
        metadata={"selected_inp": str(reaction_dir / "job.inp")},
    )

    if writer == "pending_cancel":
        assert cancel(tmp_path, entry.queue_id, expected_entry=entry) is not None
    else:
        running = dequeue_next(tmp_path)
        assert running is not None
        if writer == "start_like_failure":
            assert mark_failed(
                tmp_path,
                entry.queue_id,
                error="worker_start_error",
                expected_entry=running,
            )
        else:
            requested = cancel(tmp_path, entry.queue_id, expected_entry=running)
            assert requested is not None and requested.cancel_requested
            if writer == "requeue_cancel":
                assert requeue_running_entry(
                    tmp_path,
                    entry.queue_id,
                    expected_entry=requested,
                )
            else:
                assert (
                    replay_mod.reconcile_orphaned_running_entries(
                        tmp_path,
                        ignore_worker_pid=True,
                    )
                    == 1
                )

    [terminal] = list_queue(tmp_path)
    assert terminal.status.value == expected_status
    assert replay_mod.terminal_replay_marker_from_entry(terminal) is not None
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))

    def current_entries(_cfg: AppConfig) -> list[tuple[Path, QueueEntry]]:
        return [(tmp_path, current) for current in list_queue(tmp_path)]

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            side_effect=current_entries,
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            worker_tracking_mod,
            "upsert_terminal_job_record",
            return_value=True,
        ) as upsert,
        patch.object(
            worker_tracking_mod,
            "notify_terminal_job_from_state",
            return_value=False,
        ) as notify,
    ):
        replay_mod.reconcile_worker_state(MagicMock(cfg=cfg, admission_root=tmp_path))
        replay_mod.reconcile_worker_state(MagicMock(cfg=cfg, admission_root=tmp_path))

    upsert.assert_called_once()
    notify.assert_called_once()
    [closed] = list_queue(tmp_path)
    assert replay_mod.terminal_replay_marker_from_entry(closed) is None
    state = load_state(reaction_dir)
    assert state is not None
    assert state["job_id"] == entry.task_id
    final_result = state["final_result"]
    assert final_result is not None
    assert final_result["status"] == expected_status


@pytest.mark.parametrize("bad_version", [None, True, 2, "1", [], {}])
def test_terminal_replay_marker_rejects_malformed_version(bad_version: object) -> None:
    entry = QueueEntry(
        queue_id="queue-corrupt-marker",
        app_name="orca_auto_orca",
        task_id="task-corrupt-marker",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.FAILED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {"version": bad_version},
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is None


@pytest.mark.parametrize("bad_observed_state", [None, [], {}, {"present": "yes"}])
def test_terminal_replay_marker_rejects_malformed_state_fingerprint(
    bad_observed_state: object,
) -> None:
    entry = QueueEntry(
        queue_id="queue-corrupt-fingerprint",
        app_name="orca_auto_orca",
        task_id="task-corrupt-fingerprint",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.FAILED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {
                "version": 1,
                "task_id": "task-corrupt-fingerprint",
                "observed_state": bad_observed_state,
            },
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is None


@pytest.mark.parametrize(
    ("marker_task_id", "marker_status"),
    [
        ("other-task", STATUS_FAILED),
        ("task-replay", STATUS_RUNNING),
        ("task-replay", ""),
    ],
)
def test_terminal_replay_marker_rejects_unbound_identity_or_nonterminal_status(
    marker_task_id: str,
    marker_status: str,
) -> None:
    entry = QueueEntry(
        queue_id="queue-invalid-binding",
        app_name="orca_auto_orca",
        task_id="task-replay",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.FAILED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {
                "version": 1,
                "task_id": marker_task_id,
                "selected_inp": "",
                "status": marker_status,
                "error": "exit_code=1",
                "observed_state": {
                    "present": False,
                    "readable": True,
                    "job_id": "",
                    "run_id": "",
                    "terminal_status": "",
                },
            },
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is None


def test_terminal_replay_marker_allows_durable_status_correction() -> None:
    entry = QueueEntry(
        queue_id="queue-corrected-status",
        app_name="orca_auto_orca",
        task_id="task-corrected-status",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.COMPLETED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {
                "version": 1,
                "task_id": "task-corrected-status",
                "selected_inp": "",
                # A cancellation replay may discover an already-completed state
                # and correct the queue before it clears the original marker.
                "status": STATUS_CANCELLED,
                "error": "cancel_requested",
                "observed_state": {
                    "present": False,
                    "readable": True,
                    "job_id": "",
                    "run_id": "",
                    "terminal_status": "",
                },
            },
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is not None


@pytest.mark.parametrize("blocked_kind", ["fence_only", "malformed", "conflict"])
def test_repair_blocked_terminal_never_uses_observed_active_edge(
    tmp_path: Path,
    blocked_kind: str,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    metadata: dict[str, Any] = {"reaction_dir": str(reaction_dir)}
    if blocked_kind == "fence_only":
        metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] = True
    elif blocked_kind == "malformed":
        metadata["orca_terminal_replay"] = {"version": 2}
    else:
        metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] = True
        metadata["orca_terminal_replay"] = terminal_replay_marker(
            reaction_dir=str(reaction_dir),
            task_id="task-replay",
            selected_inp="",
            status=STATUS_FAILED,
            error="administrative_fence",
        )
    entry = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.FAILED),
        metadata=metadata,
    )
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(replay_mod, "record_failed_run_state") as record_failed,
        patch.object(replay_mod, "update_terminal") as update,
        patch.object(worker_tracking_mod, "upsert_terminal_job_record") as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
        patch.object(replay_mod, "_clear_terminal_replay_marker") as clear_marker,
    ):
        _run_terminal_replay(
            worker,
            tmp_path,
            entry,
            previous_status=STATUS_RUNNING,
        )

    record_failed.assert_not_called()
    update.assert_not_called()
    upsert.assert_not_called()
    notify.assert_not_called()
    clear_marker.assert_not_called()
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == STATUS_FAILED
    assert replay_mod.get_replay_state(worker).pending_replays == {}


def test_terminal_replay_with_empty_reaction_dir_never_resolves_workspace() -> None:
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=Path("/tmp/queue"),
        queue_id="queue-empty-reaction",
        reaction_dir="",
        reaction_key="",
        task_id="task-empty-reaction",
        observed_status=STATUS_FAILED,
        selected_inp="",
        error="exit_code=1",
    )

    with (
        patch.object(replay_mod, "record_failed_run_state") as record_failed,
        patch.object(worker_tracking_mod, "upsert_terminal_job_record") as upsert,
        pytest.raises(RuntimeError, match="no reaction directory"),
    ):
        replay_mod._prepare_terminal_replay_work_item(item)

    record_failed.assert_not_called()
    upsert.assert_not_called()
    assert replay_mod._pending_replay_state_is_superseded(item)


def test_terminal_replay_retries_failed_notification_until_marker_is_durable(
    tmp_path: Path,
) -> None:
    (tmp_path / "rxn").mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.COMPLETED)
    cfg = AppConfig(
        runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)),
        messenger=MessengerConfig(
            discord=DiscordConfig(bot_token="token", default_channel_id="123")
        ),
    )
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    state = {
        "job_id": entry.task_id,
        "final_result": {"status": "completed"},
    }

    with (
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
        patch.object(
            worker_tracking_mod, "notify_terminal_job_from_state", return_value=False
        ) as notify,
        patch.object(replay_mod, "load_state", return_value=state),
    ):
        _run_terminal_replay(
            worker,
            tmp_path,
            entry,
            previous_status=STATUS_RUNNING,
        )
        _run_terminal_replay(worker, tmp_path, entry)

    assert notify.call_count == 2
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == "running"


def test_terminal_replay_uses_selected_discord_provider_for_durability(
    tmp_path: Path,
) -> None:
    (tmp_path / "rxn").mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.COMPLETED)
    cfg = AppConfig(
        runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)),
        messenger=MessengerConfig(
            provider="discord",
            discord=DiscordConfig(bot_token="secret-token", default_channel_id="123"),
        ),
    )
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    state = {
        "job_id": entry.task_id,
        "final_result": {"status": "completed"},
    }

    with (
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
        patch.object(
            worker_tracking_mod, "notify_terminal_job_from_state", return_value=False
        ) as notify,
        patch.object(replay_mod, "load_state", return_value=state),
    ):
        _run_terminal_replay(
            worker,
            tmp_path,
            entry,
            previous_status=STATUS_RUNNING,
        )
        _run_terminal_replay(worker, tmp_path, entry)

    assert notify.call_count == 2
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == "running"


def test_terminal_replay_retries_when_job_record_artifacts_are_not_ready(
    tmp_path: Path,
) -> None:
    (tmp_path / "rxn").mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.COMPLETED)
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=False),
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
    ):
        _run_terminal_replay(
            worker,
            tmp_path,
            entry,
            previous_status=STATUS_RUNNING,
        )

    notify.assert_not_called()
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == "running"


def test_terminal_replay_finalizes_cancelled_state_before_side_effects(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED)
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            return_value=("run-cancelled", STATUS_CANCELLED),
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=True) as update_terminal,
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
    ):
        _run_terminal_replay(
            worker,
            tmp_path,
            entry,
            previous_status=STATUS_RUNNING,
        )

    record_cancelled.assert_called_once()
    assert record_cancelled.call_args.args == (reaction_dir.resolve(),)
    assert record_cancelled.call_args.kwargs["fallback_job_id"] == entry.task_id
    assert record_cancelled.call_args.kwargs["selected_inp"] == ""
    assert record_cancelled.call_args.kwargs["observed_state"] is not None
    update_terminal.assert_called_once_with(
        tmp_path.resolve(),
        entry.queue_id,
        STATUS_CANCELLED,
        run_id="run-cancelled",
        expected_task_id=entry.task_id,
    )
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == QueueStatus.CANCELLED.value


def test_terminal_replay_corrects_cancelled_queue_to_existing_completed_state(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED)
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            return_value=("run-completed", STATUS_COMPLETED),
        ),
        patch.object(replay_mod, "update_terminal", return_value=True) as update_terminal,
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
    ):
        _run_terminal_replay(
            worker,
            tmp_path,
            entry,
            previous_status=STATUS_RUNNING,
        )

    update_terminal.assert_called_once_with(
        tmp_path.resolve(),
        entry.queue_id,
        STATUS_COMPLETED,
        run_id="run-completed",
        expected_task_id=entry.task_id,
    )
    key = (str(tmp_path.resolve()), entry.queue_id)
    assert _reconcile_statuses(worker)[key] == STATUS_COMPLETED


def test_terminal_replay_observes_pending_to_cancelled_transition(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    pending = _terminal_replay_entry(tmp_path, QueueStatus.PENDING)
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(
            worker_tracking_mod, "upsert_terminal_job_record", return_value=True
        ) as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
        patch.object(replay_mod, "update_terminal", return_value=True),
    ):
        _run_terminal_replay(worker, tmp_path, pending)
        upsert.assert_not_called()

        cancelled = replace(
            pending,
            status=QueueStatus.CANCELLED,
            cancel_requested=True,
        )
        _run_terminal_replay(worker, tmp_path, cancelled)
        _run_terminal_replay(worker, tmp_path, cancelled)

    # Replay the transition once, then retain the successful terminal status as
    # the long-running worker's cursor rather than duplicating side effects.
    upsert.assert_called_once_with(
        cfg,
        str(reaction_dir),
        fallback_job_id=pending.task_id,
        expected_job_id=pending.task_id,
    )
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == pending.task_id
    assert written["status"] == STATUS_CANCELLED


def test_terminal_replay_skips_superseded_cancelled_generation(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    old_queue_root = tmp_path / "old-queue"
    current_queue_root = tmp_path / "current-queue"
    old_queue_root.mkdir()
    current_queue_root.mkdir()
    old_cancelled = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED),
        queue_id="queue-z",
        task_id="task-a",
        enqueued_at="2099-07-09T00:00:00+00:00",
    )
    current_running = replace(
        old_cancelled,
        queue_id="queue-0",
        task_id="task-b",
        status=QueueStatus.RUNNING,
        cancel_requested=False,
        enqueued_at="",
    )
    current_state = new_state(reaction_dir, reaction_dir / "task-b.inp", max_retries=3)
    current_state["job_id"] = "task-b"
    current_state["status"] = STATUS_RUNNING
    save_state(reaction_dir, current_state)
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    entries = [
        (old_queue_root, old_cancelled),
        (current_queue_root, current_running),
    ]

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            return_value=entries,
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(worker_tracking_mod, "upsert_terminal_job_record") as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
    ):
        replay_mod.reconcile_worker_state(worker)
        upsert.assert_not_called()
        notify.assert_not_called()

        finalize_state(
            reaction_dir,
            current_state,
            status=STATUS_COMPLETED,
            final_result={
                "status": STATUS_COMPLETED,
                "reason": "normal_termination",
                "completed_at": "2026-07-10T00:00:00+00:00",
            },
        )
        entries[1] = (
            current_queue_root,
            replace(current_running, status=QueueStatus.COMPLETED),
        )
        replay_mod.reconcile_worker_state(worker)

    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["status"] == STATUS_COMPLETED
    assert written["run_id"] == current_state["run_id"]
    upsert.assert_called_once_with(
        cfg,
        str(reaction_dir),
        fallback_job_id="task-b",
        expected_job_id="task-b",
    )
    notify.assert_called_once_with(
        cfg,
        str(reaction_dir),
        expected_job_id="task-b",
    )


def test_terminal_owner_switches_from_terminal_owner_to_seen_active_generation(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    active_a = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.RUNNING),
        queue_id="queue-a",
        task_id="task-a",
        enqueued_at="2026-07-10T00:00:00+00:00",
    )
    failed_b = replace(
        active_a,
        queue_id="queue-b",
        task_id="task-b",
        status=QueueStatus.FAILED,
        enqueued_at="2026-07-11T00:00:00+00:00",
        error="lock failed",
    )
    owner_a = (str(root_a.resolve()), active_a.queue_id)
    owner_b = (str(root_b.resolve()), failed_b.queue_id)
    reaction_key = str(reaction_dir.resolve())
    entries = [(root_a, active_a), (root_b, failed_b)]
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    replay_mod.get_replay_state(worker).generation_owners = {reaction_key: owner_b}
    replay_mod.get_replay_state(worker).generation_owner_active = {reaction_key: True}
    replay_mod.get_replay_state(worker).reconcile_statuses = {
        owner_a: STATUS_RUNNING,
        owner_b: STATUS_RUNNING,
    }

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            return_value=entries,
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(replay_mod, "record_failed_run_state") as record_failed,
        patch.object(worker_tracking_mod, "upsert_terminal_job_record") as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
    ):
        replay_mod.reconcile_worker_state(worker)

    assert replay_mod.get_replay_state(worker).generation_owners[reaction_key] == owner_a
    assert replay_mod.get_replay_state(worker).generation_owner_active[reaction_key] is True
    record_failed.assert_not_called()
    upsert.assert_not_called()
    notify.assert_not_called()


def test_terminal_owner_uses_current_state_over_future_or_blank_timestamps(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    old_root.mkdir()
    new_root.mkdir()
    old_cancelled = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED),
        queue_id="queue-old",
        task_id="task-old",
        enqueued_at="2099-07-10T00:00:00+00:00",
    )
    new_cancelled = replace(
        old_cancelled,
        queue_id="queue-new",
        task_id="task-new",
        enqueued_at="",
    )
    state = new_state(reaction_dir, reaction_dir / "new.inp", max_retries=0)
    state["job_id"] = new_cancelled.task_id
    save_state(reaction_dir, state)
    report_json_path(reaction_dir).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=old_cancelled.task_id,
                run_id="run-old",
                reaction_dir=str(reaction_dir),
                status=STATUS_COMPLETED,
                final_result={"status": STATUS_COMPLETED},
            )
        ),
        encoding="utf-8",
    )
    entries = [(old_root, old_cancelled), (new_root, new_cancelled)]
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    replay_mod.get_replay_state(worker).reconcile_statuses = {
        (str(root.resolve()), entry.queue_id): STATUS_RUNNING for root, entry in entries
    }

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            return_value=entries,
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            wraps=replay_mod.record_cancelled_run_state,
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=True),
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
    ):
        replay_mod.reconcile_worker_state(worker)

    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == new_cancelled.task_id
    assert written["status"] == STATUS_CANCELLED
    record_cancelled.assert_called_once()
    assert record_cancelled.call_args.args == (reaction_dir.resolve(),)
    assert record_cancelled.call_args.kwargs["fallback_job_id"] == new_cancelled.task_id
    assert record_cancelled.call_args.kwargs["selected_inp"] == ""
    assert record_cancelled.call_args.kwargs["observed_state"] is not None
    reaction_key = str(reaction_dir.resolve())
    assert replay_mod.get_replay_state(worker).generation_owners[reaction_key] == (
        str(new_root.resolve()),
        new_cancelled.queue_id,
    )
    assert (
        _reconcile_statuses(worker)[(str(old_root.resolve()), old_cancelled.queue_id)]
        == STATUS_RUNNING
    )


def test_ambiguous_terminal_generations_retry_when_state_identity_appears(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    cancelled_a = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED),
        queue_id="queue-a",
        task_id="task-a",
        enqueued_at="2099-07-10T00:00:00+00:00",
    )
    cancelled_b = replace(
        cancelled_a,
        queue_id="queue-b",
        task_id="task-b",
        enqueued_at="",
    )
    entries = [(root_a, cancelled_a), (root_b, cancelled_b)]
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    replay_mod.get_replay_state(worker).reconcile_statuses = {
        (str(root.resolve()), entry.queue_id): STATUS_RUNNING for root, entry in entries
    }

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            return_value=entries,
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            wraps=replay_mod.record_cancelled_run_state,
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=True),
        patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
    ):
        replay_mod.reconcile_worker_state(worker)
        record_cancelled.assert_not_called()
        assert all(status == STATUS_RUNNING for status in _reconcile_statuses(worker).values())

        state = new_state(reaction_dir, reaction_dir / "b.inp", max_retries=0)
        state["job_id"] = cancelled_b.task_id
        save_state(reaction_dir, state)
        replay_mod.reconcile_worker_state(worker)

    record_cancelled.assert_called_once()
    assert record_cancelled.call_args.args == (reaction_dir.resolve(),)
    assert record_cancelled.call_args.kwargs["fallback_job_id"] == cancelled_b.task_id
    assert record_cancelled.call_args.kwargs["selected_inp"] == ""
    assert record_cancelled.call_args.kwargs["observed_state"] is not None


def test_terminal_replay_snapshot_survives_entry_disappearance(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED)
    entries = [(tmp_path, entry)]
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    replay_mod.get_replay_state(worker).reconcile_statuses = {
        (str(tmp_path.resolve()), entry.queue_id): STATUS_RUNNING
    }

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            side_effect=[entries, entries, [], []],
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            return_value=("run-cancelled", STATUS_CANCELLED),
        ),
        patch.object(replay_mod, "update_terminal", return_value=False) as update,
        patch.object(
            worker_tracking_mod,
            "upsert_terminal_job_record",
            side_effect=[False, True],
        ) as upsert,
        patch.object(
            worker_tracking_mod,
            "notify_terminal_job_from_state",
            return_value=False,
        ) as notify,
    ):
        replay_mod.reconcile_worker_state(worker)
        pending = replay_mod.get_replay_state(worker).pending_replays
        assert len(pending) == 1

        replay_mod.reconcile_worker_state(worker)

    update.assert_called_once()
    assert upsert.call_count == 2
    notify.assert_called_once_with(
        cfg,
        str(reaction_dir),
        expected_job_id=entry.task_id,
    )
    assert replay_mod.get_replay_state(worker).pending_replays == {}


def test_terminal_replay_snapshot_retries_state_preparation_after_disappearance(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    entry = _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED)
    entries = [(tmp_path, entry)]
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    replay_mod.get_replay_state(worker).reconcile_statuses = {
        (str(tmp_path.resolve()), entry.queue_id): STATUS_RUNNING
    }

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            side_effect=[entries, entries, [], []],
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            side_effect=[OSError("state write failed"), ("run-cancelled", STATUS_CANCELLED)],
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal") as update,
        patch.object(
            worker_tracking_mod,
            "upsert_terminal_job_record",
            return_value=True,
        ) as upsert,
        patch.object(
            worker_tracking_mod,
            "notify_terminal_job_from_state",
            return_value=False,
        ) as notify,
    ):
        replay_mod.reconcile_worker_state(worker)
        pending = replay_mod.get_replay_state(worker).pending_replays
        assert len(pending) == 1
        assert not next(iter(pending.values())).state_prepared

        replay_mod.reconcile_worker_state(worker)

    assert record_cancelled.call_count == 2
    update.assert_not_called()
    upsert.assert_called_once_with(
        cfg,
        str(reaction_dir),
        fallback_job_id=entry.task_id,
        expected_job_id=entry.task_id,
    )
    notify.assert_called_once_with(
        cfg,
        str(reaction_dir),
        expected_job_id=entry.task_id,
    )
    assert replay_mod.get_replay_state(worker).pending_replays == {}


def test_unprepared_terminal_replay_keeps_transition_evidence_while_entry_remains(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    running = _terminal_replay_entry(tmp_path, QueueStatus.RUNNING)
    cancelled = replace(running, status=QueueStatus.CANCELLED)
    stale = new_state(reaction_dir, reaction_dir / "old.inp", max_retries=0)
    stale["job_id"] = "task-old"
    finalize_state(
        reaction_dir,
        stale,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "old-generation"},
    )
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            side_effect=[OSError("state write failed"), ("run-current", STATUS_CANCELLED)],
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=True),
        patch.object(
            worker_tracking_mod, "upsert_terminal_job_record", return_value=True
        ) as upsert,
        patch.object(
            worker_tracking_mod,
            "notify_terminal_job_from_state",
            return_value=False,
        ) as notify,
    ):
        _run_terminal_replay(worker, tmp_path, running)
        _run_terminal_replay(worker, tmp_path, cancelled)
        pending = replay_mod.get_replay_state(worker).pending_replays
        assert len(pending) == 1
        assert not next(iter(pending.values())).state_prepared

        _run_terminal_replay(worker, tmp_path, cancelled)

    assert record_cancelled.call_count == 2
    upsert.assert_called_once_with(
        cfg,
        str(reaction_dir),
        fallback_job_id=cancelled.task_id,
        expected_job_id=cancelled.task_id,
    )
    notify.assert_called_once_with(
        cfg,
        str(reaction_dir),
        expected_job_id=cancelled.task_id,
    )
    assert replay_mod.get_replay_state(worker).pending_replays == {}
    assert replay_mod.get_replay_state(worker).generation_owners[str(reaction_dir.resolve())] == (
        str(tmp_path.resolve()),
        cancelled.queue_id,
    )


def test_prepared_terminal_replay_is_dropped_when_entry_state_is_superseded(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    running = _terminal_replay_entry(tmp_path, QueueStatus.RUNNING)
    cancelled = replace(running, status=QueueStatus.CANCELLED)
    current = new_state(reaction_dir, reaction_dir / "current.inp", max_retries=0)
    current["job_id"] = cancelled.task_id
    save_state(reaction_dir, current)
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            wraps=replay_mod.record_cancelled_run_state,
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=True),
        patch.object(
            worker_tracking_mod, "upsert_terminal_job_record", return_value=False
        ) as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
    ):
        _run_terminal_replay(worker, tmp_path, running)
        _run_terminal_replay(worker, tmp_path, cancelled)
        pending = replay_mod.get_replay_state(worker).pending_replays
        assert len(pending) == 1
        assert next(iter(pending.values())).state_prepared

        newer = new_state(reaction_dir, reaction_dir / "newer.inp", max_retries=0)
        newer["job_id"] = "task-newer"
        newer["status"] = STATUS_RUNNING
        save_state(reaction_dir, newer)
        _run_terminal_replay(worker, tmp_path, cancelled)

    record_cancelled.assert_called_once()
    upsert.assert_called_once()
    notify.assert_not_called()
    assert replay_mod.get_replay_state(worker).pending_replays == {}
    key = (str(tmp_path.resolve()), cancelled.queue_id)
    assert _reconcile_statuses(worker)[key] == STATUS_CANCELLED
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-newer"
    assert written["status"] == STATUS_RUNNING


def test_durable_terminal_replay_drops_old_finalizer_after_newer_terminal_state(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    state_a = new_state(reaction_dir, reaction_dir / "a.inp", max_retries=0)
    state_a["job_id"] = "task-a"
    finalize_state(
        reaction_dir,
        state_a,
        status=STATUS_CANCELLED,
        final_result={"status": STATUS_CANCELLED, "reason": "cancel_requested"},
    )
    marker = terminal_replay_marker(
        reaction_dir=str(reaction_dir),
        task_id="task-a",
        selected_inp=str(reaction_dir / "a.inp"),
        status=STATUS_CANCELLED,
        error="cancel_requested",
    )
    old_entry = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED),
        task_id="task-a",
        metadata={
            "reaction_dir": str(reaction_dir),
            "orca_terminal_replay": marker,
        },
    )
    state_b = new_state(reaction_dir, reaction_dir / "b.inp", max_retries=0)
    state_b["job_id"] = "task-b"
    finalize_state(
        reaction_dir,
        state_b,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)

    with (
        patch.object(replay_mod, "record_cancelled_run_state") as record_cancelled,
        patch.object(worker_tracking_mod, "upsert_terminal_job_record") as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
    ):
        _run_terminal_replay(worker, tmp_path, old_entry)

    record_cancelled.assert_not_called()
    upsert.assert_not_called()
    notify.assert_not_called()
    assert replay_mod.get_replay_state(worker).pending_replays == {}
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["status"] == STATUS_COMPLETED


def test_new_active_generation_supersedes_disappeared_terminal_replay(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    old_root.mkdir()
    new_root.mkdir()
    old_cancelled = replace(
        _terminal_replay_entry(tmp_path, QueueStatus.CANCELLED),
        queue_id="queue-old",
        task_id="task-old",
    )
    new_running = replace(
        old_cancelled,
        queue_id="queue-new",
        task_id="task-new",
        status=QueueStatus.RUNNING,
    )
    old_entries = [(old_root, old_cancelled)]
    new_entries = [(new_root, new_running)]
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))
    worker = MagicMock(cfg=cfg, admission_root=tmp_path)
    replay_mod.get_replay_state(worker).reconcile_statuses = {
        (str(old_root.resolve()), old_cancelled.queue_id): STATUS_RUNNING
    }

    with (
        patch.object(replay_mod, "recover_orphaned_engine_slots"),
        patch.object(
            replay_mod,
            "queue_entries_with_roots",
            side_effect=[old_entries, old_entries, new_entries, new_entries],
        ),
        patch.object(
            replay_mod,
            "live_queue_slot_keys_for_slots",
            return_value=(set(), set()),
        ),
        patch.object(replay_mod, "reconcile_orphaned_process_entries"),
        patch.object(
            replay_mod,
            "record_cancelled_run_state",
            return_value=("run-old", STATUS_CANCELLED),
        ) as record_cancelled,
        patch.object(replay_mod, "update_terminal", return_value=False),
        patch.object(
            worker_tracking_mod,
            "upsert_terminal_job_record",
            return_value=False,
        ) as upsert,
        patch.object(worker_tracking_mod, "notify_terminal_job_from_state") as notify,
    ):
        replay_mod.reconcile_worker_state(worker)
        assert len(replay_mod.get_replay_state(worker).pending_replays) == 1

        replay_mod.reconcile_worker_state(worker)

    record_cancelled.assert_called_once()
    upsert.assert_called_once()
    notify.assert_not_called()
    assert replay_mod.get_replay_state(worker).pending_replays == {}
    assert replay_mod.get_replay_state(worker).generation_owners[str(reaction_dir.resolve())] == (
        str(new_root.resolve()),
        new_running.queue_id,
    )


def test_record_cancelled_run_state_synthesizes_missing_terminal_state(tmp_path: Path) -> None:
    selected_inp = tmp_path / "job.inp"

    run_id, terminal_status = _record_cancelled_run_state(
        tmp_path,
        fallback_job_id="task-cancelled",
        selected_inp=str(selected_inp),
    )

    assert run_id
    assert terminal_status == STATUS_CANCELLED
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-cancelled"
    assert written["selected_inp"] == str(selected_inp)
    assert written["status"] == STATUS_CANCELLED
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == STATUS_CANCELLED


def test_record_cancelled_run_state_normalizes_nonterminal_final_result(tmp_path: Path) -> None:
    state = new_state(tmp_path, tmp_path / "job.inp", max_retries=2)
    state["job_id"] = "task-cancelled"
    state["final_result"] = {"status": STATUS_RUNNING, "reason": "malformed"}
    save_state(tmp_path, state)

    run_id, terminal_status = _record_cancelled_run_state(
        tmp_path,
        fallback_job_id="task-cancelled",
        selected_inp=str(tmp_path / "job.inp"),
    )

    assert run_id == state["run_id"]
    assert terminal_status == STATUS_CANCELLED
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-cancelled"
    assert written["status"] == STATUS_CANCELLED
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == STATUS_CANCELLED
    assert written["final_result"]["reason"] == "cancel_requested"


def test_record_failed_run_state_normalizes_nonterminal_final_result(tmp_path: Path) -> None:
    state = new_state(tmp_path, tmp_path / "job.inp", max_retries=2)
    state["job_id"] = "task-failed"
    state["final_result"] = {"status": STATUS_RUNNING, "reason": "malformed"}
    save_state(tmp_path, state)

    run_id, terminal_status = _record_failed_run_state(
        tmp_path,
        fallback_job_id="task-failed",
        selected_inp=str(tmp_path / "job.inp"),
        reason="exit_code=9",
    )

    assert run_id == state["run_id"]
    assert terminal_status == "failed"
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-failed"
    assert written["status"] == "failed"
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "failed"
    assert written["final_result"]["reason"] == "exit_code=9"


def test_terminal_state_helpers_fail_closed_on_active_generation_mismatch(
    tmp_path: Path,
) -> None:
    state = new_state(tmp_path, tmp_path / "task-b.inp", max_retries=2)
    state["job_id"] = "task-b"
    state["status"] = STATUS_RUNNING
    save_state(tmp_path, state)
    before = state_path(tmp_path).read_bytes()

    with pytest.raises(RuntimeError, match="different active generation"):
        _record_cancelled_run_state(
            tmp_path,
            fallback_job_id="task-a",
            selected_inp=str(tmp_path / "task-a.inp"),
        )
    assert state_path(tmp_path).read_bytes() == before

    with pytest.raises(RuntimeError, match="different active generation"):
        _record_failed_run_state(
            tmp_path,
            fallback_job_id="task-a",
            selected_inp=str(tmp_path / "task-a.inp"),
            reason="exit_code=1",
        )
    assert state_path(tmp_path).read_bytes() == before


def test_terminal_state_helper_cannot_write_while_current_run_lock_is_held(
    tmp_path: Path,
) -> None:
    state = new_state(tmp_path, tmp_path / "task-a.inp", max_retries=2)
    state["job_id"] = "task-a"
    state["status"] = STATUS_RUNNING
    save_state(tmp_path, state)
    before = state_path(tmp_path).read_bytes()

    with replay_mod.acquire_run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            _record_failed_run_state(
                tmp_path,
                fallback_job_id="task-a",
                selected_inp=str(tmp_path / "task-a.inp"),
                reason="exit_code=1",
            )

    assert state_path(tmp_path).read_bytes() == before


def test_terminal_state_cas_rejects_changed_terminal_fingerprint(tmp_path: Path) -> None:
    state_a = new_state(tmp_path, tmp_path / "a.inp", max_retries=0)
    state_a["job_id"] = "task-a"
    finalize_state(
        tmp_path,
        state_a,
        status=STATUS_CANCELLED,
        final_result={"status": STATUS_CANCELLED, "reason": "cancel_requested"},
    )
    observed = replay_mod.load_state_generation_fingerprint(tmp_path)

    state_b = new_state(tmp_path, tmp_path / "b.inp", max_retries=0)
    state_b["job_id"] = "task-b"
    finalize_state(
        tmp_path,
        state_b,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    before = state_path(tmp_path).read_bytes()

    with pytest.raises(RuntimeError, match="superseded"):
        _record_failed_run_state(
            tmp_path,
            fallback_job_id="task-a",
            selected_inp=str(tmp_path / "a.inp"),
            reason="exit_code=1",
            observed_state=observed,
        )

    assert state_path(tmp_path).read_bytes() == before
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-b"


def test_terminal_replay_keeps_marker_when_state_identity_is_unreadable(
    tmp_path: Path,
) -> None:
    observed = replay_mod.StateGenerationFingerprint(
        present=True,
        readable=True,
        job_id="task-old",
        run_id="run-old",
        terminal_status=STATUS_COMPLETED,
    )
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-unreadable",
        reaction_dir=str(tmp_path),
        reaction_key=str(tmp_path.resolve()),
        task_id="task-current",
        observed_status=STATUS_FAILED,
        selected_inp="",
        error="exit_code=1",
        observed_state=observed,
    )

    with patch.object(
        replay_mod,
        "load_state_generation_fingerprint",
        return_value=replay_mod.StateGenerationFingerprint(
            present=True,
            readable=False,
        ),
    ):
        assert not replay_mod._pending_replay_state_is_superseded(item)

    unreadable_observed = replace(
        item,
        observed_state=replay_mod.StateGenerationFingerprint(
            present=True,
            readable=False,
        ),
    )
    with patch.object(
        replay_mod,
        "load_state_generation_fingerprint",
        return_value=replay_mod.StateGenerationFingerprint(
            present=True,
            readable=True,
            job_id="task-other",
            run_id="run-other",
        ),
    ):
        assert not replay_mod._pending_replay_state_is_superseded(unreadable_observed)


def test_terminal_state_cas_rejects_same_task_new_run_id(tmp_path: Path) -> None:
    first = new_state(tmp_path, tmp_path / "same.inp", max_retries=0)
    first["job_id"] = "task-same"
    save_state(tmp_path, first)
    observed = replay_mod.load_state_generation_fingerprint(tmp_path)

    second = new_state(tmp_path, tmp_path / "same.inp", max_retries=0)
    second["job_id"] = "task-same"
    save_state(tmp_path, second)
    before = state_path(tmp_path).read_bytes()
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-same-task",
        reaction_dir=str(tmp_path),
        reaction_key=str(tmp_path.resolve()),
        task_id="task-same",
        observed_status=STATUS_FAILED,
        selected_inp=str(tmp_path / "same.inp"),
        error="exit_code=1",
        observed_state=observed,
    )

    assert replay_mod._pending_replay_state_is_superseded(item)
    with pytest.raises(RuntimeError, match="newer run"):
        _record_failed_run_state(
            tmp_path,
            fallback_job_id="task-same",
            selected_inp=str(tmp_path / "same.inp"),
            reason="exit_code=1",
            observed_state=observed,
        )

    assert state_path(tmp_path).read_bytes() == before
    written = load_state(tmp_path)
    assert written is not None
    assert written["run_id"] == second["run_id"]


def test_terminal_state_cas_rejects_expected_task_run_after_different_observation(
    tmp_path: Path,
) -> None:
    previous = new_state(tmp_path, tmp_path / "previous.inp", max_retries=0)
    previous["job_id"] = "task-a"
    finalize_state(
        tmp_path,
        previous,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    observed = replay_mod.load_state_generation_fingerprint(tmp_path)

    current = new_state(tmp_path, tmp_path / "current.inp", max_retries=0)
    current["job_id"] = "task-b"
    current["status"] = STATUS_RUNNING
    save_state(tmp_path, current)
    before = state_path(tmp_path).read_bytes()
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-task-b",
        reaction_dir=str(tmp_path),
        reaction_key=str(tmp_path.resolve()),
        task_id="task-b",
        observed_status=STATUS_FAILED,
        selected_inp=str(tmp_path / "current.inp"),
        error="exit_code=1",
        observed_state=observed,
    )

    assert replay_mod._pending_replay_state_is_superseded(item)
    with pytest.raises(RuntimeError, match="new run for the expected task"):
        replay_mod._prepare_terminal_replay_work_item(item)

    assert state_path(tmp_path).read_bytes() == before
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["run_id"] == current["run_id"]
    assert written["status"] == STATUS_RUNNING


def test_terminal_upsert_filters_previous_generation_report(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "task-b.inp"
    _record_cancelled_run_state(
        reaction_dir,
        fallback_job_id="task-b",
        selected_inp=str(selected_inp),
    )
    report_json_path(reaction_dir).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="task-a",
                run_id="run-a",
                reaction_dir=str(reaction_dir),
                status=STATUS_COMPLETED,
                final_result={"status": STATUS_COMPLETED},
            )
        ),
        encoding="utf-8",
    )
    cfg = AppConfig(runtime=RetryRuntimeConfig(allowed_root=str(tmp_path)))

    with patch.object(worker_tracking_mod, "upsert_job_record") as upsert:
        assert worker_tracking_mod.upsert_terminal_job_record(
            cfg,
            str(reaction_dir),
            fallback_job_id="task-b",
        )

    assert upsert.call_args.kwargs["job_id"] == "task-b"
    assert upsert.call_args.kwargs["status"] == STATUS_CANCELLED


def test_record_cancelled_run_state_writes_terminal_cancelled(tmp_path: Path) -> None:
    # A cancelled run is stopped by a signal and never writes its own terminal
    # result, so the worker records a cancelled outcome on its behalf. Without it
    # the run state lingers as "running" (job never leaves the list, no notify).
    state = new_state(tmp_path, tmp_path / "job.inp", max_retries=3)
    state["status"] = "running"
    save_state(tmp_path, state)

    run_id, terminal_status = _record_cancelled_run_state(tmp_path)

    assert run_id == state["run_id"]
    assert terminal_status == "cancelled"
    written = load_state(tmp_path)
    assert written is not None
    assert written["status"] == "cancelled"
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "cancelled"
    assert written["final_result"]["reason"] == "cancel_requested"


def test_record_cancelled_run_state_keeps_existing_terminal_result(tmp_path: Path) -> None:
    # If a real terminal outcome landed just before cancellation, don't clobber it.
    state = new_state(tmp_path, tmp_path / "job.inp", max_retries=3)
    finalize_state(
        tmp_path,
        state,
        status="completed",
        final_result={
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "t",
            "last_out_path": None,
        },
    )

    run_id, terminal_status = _record_cancelled_run_state(tmp_path)

    # The pre-existing terminal outcome is preserved and reported back so the
    # caller can reconcile the queue entry to "completed" instead of "cancelled".
    assert run_id == state["run_id"]
    assert terminal_status == "completed"
    written = load_state(tmp_path)
    assert written is not None
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "completed"
