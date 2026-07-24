import json
import os
import tempfile
import unittest
from dataclasses import replace
from itertools import count
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.engine import ENGINE_DEFINITION
from orca_auto.orca.queue.adapter import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    DuplicateEntryError,
    cancel,
    clear_terminal,
    dequeue_next,
    enqueue,
    get_active_entry_for_reaction_dir,
    get_cancel_requested,
    list_queue,
    mark_completed,
    mark_failed,
    queue_entry_force,
    queue_entry_reaction_dir,
    queue_entry_run_id,
    reconcile_orphaned_running_entries,
    update_metadata,
)
from orca_auto.orca.state import finalize_state, load_state, new_state, report_json_path
from tests.engine_artifact_helpers import orca_artifact_payload


class TestQueueStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _find_entry(self, queue_id: str) -> QueueEntry | None:
        for entry in list_queue(self.root):
            if entry.queue_id == queue_id:
                return entry
        return None

    def _finish_terminal_replay(self, queue_id: str) -> None:
        """Model the worker completing side effects and clearing its replay claim."""
        self.assertTrue(
            update_metadata(
                self.root,
                queue_id,
                {TERMINAL_REPLAY_METADATA_KEY: None},
            )
        )

    # -- enqueue / basic flow -------------------------------------------

    def test_enqueue_creates_entry(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        self.assertEqual(entry.status, QueueStatus.PENDING)
        self.assertTrue(entry.queue_id.startswith("q_"))
        self.assertEqual(entry.app_name, "orca_auto_orca")
        self.assertTrue(entry.task_id.startswith("orca_"))
        self.assertEqual(entry.task_kind, "orca_run_inp")
        self.assertEqual(entry.engine, "orca")
        self.assertEqual(entry.priority, 10)
        self.assertEqual(entry.metadata["reaction_dir"], queue_entry_reaction_dir(entry))
        self.assertFalse(entry.metadata["force"])

    def test_enqueue_writes_queue_file(self) -> None:
        enqueue(self.root, str(self.root / "mol_A"))
        qp = self.root / "queue.json"
        self.assertTrue(qp.exists())
        entries = json.loads(qp.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)

    def test_enqueue_retries_generated_queue_and_task_id_collisions(self) -> None:
        generated = {
            "q": iter(["q_same", "q_same", "q_unique"]),
            "orca": iter(["orca_same", "orca_same", "orca_unique"]),
        }

        def next_token(prefix: str) -> str:
            return next(generated[prefix])

        with patch(
            "orca_auto.orca.queue.adapter.timestamped_token",
            side_effect=next_token,
        ):
            first = enqueue(self.root, str(self.root / "mol_A"))
            second = enqueue(self.root, str(self.root / "mol_B"))

        self.assertEqual((first.queue_id, first.task_id), ("q_same", "orca_same"))
        self.assertEqual((second.queue_id, second.task_id), ("q_unique", "orca_unique"))
        self.assertEqual(
            [(entry.queue_id, entry.task_id) for entry in list_queue(self.root)],
            [("q_same", "orca_same"), ("q_unique", "orca_unique")],
        )

    def test_enqueue_permanent_generated_id_collision_preserves_queue(self) -> None:
        with patch(
            "orca_auto.orca.queue.adapter.timestamped_token",
            side_effect=lambda prefix: f"{prefix}_same",
        ):
            first = enqueue(self.root, str(self.root / "mol_A"))
            queue_path = self.root / "queue.json"
            original = queue_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "unique q token"):
                enqueue(self.root, str(self.root / "mol_B"))

        self.assertEqual(queue_path.read_bytes(), original)
        [remaining] = list_queue(self.root)
        self.assertEqual(remaining.queue_id, first.queue_id)
        self.assertIn("mol_A", queue_entry_reaction_dir(remaining))

    def test_list_queue_empty(self) -> None:
        self.assertEqual(list_queue(self.root), [])

    def test_list_queue_rejects_corrupt_queue_file(self) -> None:
        qp = self.root / "queue.json"
        qp.write_text("{not valid json", encoding="utf-8")

        with self.assertRaises(queue_store.QueueStoreCorruptError):
            list_queue(self.root)

    def test_enqueue_rejects_corrupt_queue_file_without_overwriting(self) -> None:
        qp = self.root / "queue.json"
        corrupt_text = "{not valid json"
        qp.write_text(corrupt_text, encoding="utf-8")

        with self.assertRaises(queue_store.QueueStoreCorruptError):
            enqueue(self.root, str(self.root / "mol_A"))

        self.assertEqual(qp.read_text(encoding="utf-8"), corrupt_text)

    def test_list_queue_with_filter(self) -> None:
        enqueue(self.root, str(self.root / "mol_A"))
        enqueue(self.root, str(self.root / "mol_B"))
        dequeue_next(self.root)  # mol_A → running
        self.assertEqual(len(list_queue(self.root, status_filter="pending")), 1)
        self.assertEqual(len(list_queue(self.root, status_filter="running")), 1)

    # -- duplicate prevention -------------------------------------------

    def test_duplicate_active_entry_blocked(self) -> None:
        """Pending/running entries for the same dir are always blocked."""
        reaction_dir = str(self.root / "mol_A")
        entry = enqueue(self.root, reaction_dir)
        with self.assertRaises(DuplicateEntryError) as ctx:
            enqueue(self.root, reaction_dir)
        self.assertEqual(
            str(ctx.exception),
            f"Reaction directory already queued: {queue_entry_reaction_dir(entry)} "
            f"(queue_id={entry.queue_id}, status=pending). "
            "Wait for the active generation or its terminal publication to finish first.",
        )

    def test_duplicate_running_entry_blocked(self) -> None:
        enqueue(self.root, str(self.root / "mol_A"))
        dequeue_next(self.root)  # → running
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, str(self.root / "mol_A"))

    def test_duplicate_terminal_with_pending_replay_is_blocked(self) -> None:
        """A terminal queue mark still blocks while publication replay is pending."""
        entry = enqueue(self.root, str(self.root / "mol_A"))
        mark_completed(self.root, entry.queue_id)
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, str(self.root / "mol_A"))

    def test_closed_terminal_generation_allows_same_directory_without_force(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        mark_completed(self.root, entry.queue_id)
        self._finish_terminal_replay(entry.queue_id)

        new_entry = enqueue(self.root, str(self.root / "mol_A"))

        self.assertNotEqual(entry.queue_id, new_entry.queue_id)
        self.assertFalse(queue_entry_force(new_entry))

    def test_administratively_fenced_terminal_generation_blocks_successor(self) -> None:
        reaction_dir = str(self.root / "mol_A")
        entry = enqueue(self.root, reaction_dir)
        self.assertTrue(
            mark_failed(
                self.root,
                entry.queue_id,
                error="administrative_fence",
                publish_terminal_side_effects=False,
            )
        )
        [fenced] = list_queue(self.root)
        self.assertIs(fenced.metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY], True)

        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, reaction_dir)
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, reaction_dir, force=True)

    def test_duplicate_terminal_with_force_allowed(self) -> None:
        """Completed/failed entries allow re-enqueue with --force (intentional retry)."""
        entry = enqueue(self.root, str(self.root / "mol_A"))
        mark_completed(self.root, entry.queue_id)
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, str(self.root / "mol_A"), force=True)

        self._finish_terminal_replay(entry.queue_id)
        new_entry = enqueue(self.root, str(self.root / "mol_A"), force=True)
        self.assertNotEqual(entry.queue_id, new_entry.queue_id)
        self.assertTrue(queue_entry_force(new_entry))

    def test_duplicate_active_blocked_even_with_force(self) -> None:
        """Active (pending/running) entries are always blocked, even with force."""
        enqueue(self.root, str(self.root / "mol_A"))
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, str(self.root / "mol_A"), force=True)

    # -- dequeue --------------------------------------------------------

    def test_dequeue_returns_highest_priority(self) -> None:
        enqueue(self.root, str(self.root / "low"), priority=20)
        enqueue(self.root, str(self.root / "high"), priority=1)
        enqueue(self.root, str(self.root / "mid"), priority=10)

        entry = dequeue_next(self.root)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertIn("high", queue_entry_reaction_dir(entry))
        self.assertEqual(entry.status, QueueStatus.RUNNING)
        self.assertTrue(entry.started_at)

    def test_dequeue_empty_returns_none(self) -> None:
        self.assertIsNone(dequeue_next(self.root))

    # -- cancel ---------------------------------------------------------

    def test_cancel_pending(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        result = cancel(self.root, entry.queue_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, QueueStatus.CANCELLED)

    def test_cancel_running_sets_flag(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        dequeue_next(self.root)
        result = cancel(self.root, entry.queue_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.cancel_requested)
        self.assertTrue(get_cancel_requested(self.root, entry.queue_id))

    def test_cancel_terminal_returns_none(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        mark_completed(self.root, entry.queue_id)
        self.assertIsNone(cancel(self.root, entry.queue_id))

    # -- mark_completed / mark_failed -----------------------------------

    def test_mark_completed(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        dequeue_next(self.root)
        self.assertTrue(mark_completed(self.root, entry.queue_id, run_id="run_test"))
        found = self._find_entry(entry.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.COMPLETED)
        self.assertEqual(queue_entry_run_id(found), "run_test")

    def test_mark_failed_with_error(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        dequeue_next(self.root)
        self.assertTrue(mark_failed(self.root, entry.queue_id, error="exit_code=1"))
        found = self._find_entry(entry.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.FAILED)
        self.assertEqual(found.error, "exit_code=1")

    # -- clear / count ---------------------------------------------------

    def test_clear_terminal(self) -> None:
        e1 = enqueue(self.root, str(self.root / "a"))
        e2 = enqueue(self.root, str(self.root / "b"))
        enqueue(self.root, str(self.root / "c"))  # stays pending
        mark_completed(self.root, e1.queue_id)
        mark_failed(self.root, e2.queue_id)

        self.assertEqual(clear_terminal(self.root), 0)
        self._finish_terminal_replay(e1.queue_id)
        self._finish_terminal_replay(e2.queue_id)
        removed = clear_terminal(self.root)
        self.assertEqual(removed, 2)
        remaining = list_queue(self.root)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].status, QueueStatus.PENDING)

    def test_clear_terminal_retains_durable_replay_marker_outside_keep_last(self) -> None:
        protected = enqueue(self.root, str(self.root / "protected"))
        ordinary = enqueue(self.root, str(self.root / "ordinary"))
        newest = enqueue(self.root, str(self.root / "newest"))
        marker = {
            "version": 1,
            "task_id": protected.task_id,
            "selected_inp": "",
            "status": QueueStatus.COMPLETED.value,
            "error": "",
            "observed_state": {
                "present": False,
                "readable": True,
                "job_id": "",
                "run_id": "",
                "terminal_status": "",
            },
        }
        # keep_last orders terminal rows by finished_at with the random
        # queue-id hex as the only tiebreaker. Relying on the real clock for
        # the mark order makes this test hostage to timestamp ties and to
        # non-monotonic clocks (WSL2 skew corrections can step backwards
        # between marks), either of which lets the wrong row win the
        # keep_last slot. Stamp explicit, strictly increasing times so
        # "newest" is genuinely the newest, like the store-level keep_last
        # tests do.
        finish_stamps = (f"2026-03-10T00:00:{index:02d}.000000+00:00" for index in count(1))
        with patch.object(queue_store, "now_utc_iso", side_effect=lambda: next(finish_stamps)):
            mark_completed(
                self.root,
                protected.queue_id,
                metadata_update={"orca_terminal_replay": marker},
            )
            mark_failed(self.root, ordinary.queue_id)
            mark_completed(self.root, newest.queue_id)

        self.assertEqual(clear_terminal(self.root, keep_last=1), 0)
        self._finish_terminal_replay(ordinary.queue_id)
        removed = clear_terminal(self.root, keep_last=1)

        self.assertEqual(removed, 1)
        remaining_ids = {entry.queue_id for entry in list_queue(self.root)}
        self.assertEqual(remaining_ids, {protected.queue_id, newest.queue_id})

        self._finish_terminal_replay(newest.queue_id)
        self.assertEqual(clear_terminal(self.root), 1)
        [still_protected] = list_queue(self.root)
        self.assertEqual(still_protected.queue_id, protected.queue_id)
        self.assertEqual(
            still_protected.metadata.get("orca_terminal_replay"),
            marker,
        )

    def test_list_queue_can_count_running(self) -> None:
        enqueue(self.root, str(self.root / "a"))
        enqueue(self.root, str(self.root / "b"))
        dequeue_next(self.root)
        dequeue_next(self.root)
        running = [entry for entry in list_queue(self.root) if entry.status == QueueStatus.RUNNING]
        self.assertEqual(len(running), 2)

    def test_get_active_entry_for_reaction_dir_returns_pending(self) -> None:
        reaction_dir = self.root / "pending_lookup"
        entry = enqueue(self.root, str(reaction_dir))
        found = get_active_entry_for_reaction_dir(self.root, str(reaction_dir))
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.queue_id, entry.queue_id)

    def test_get_active_entry_for_reaction_dir_returns_running(self) -> None:
        reaction_dir = self.root / "running_lookup"
        entry = enqueue(self.root, str(reaction_dir))
        dequeue_next(self.root)
        found = get_active_entry_for_reaction_dir(self.root, str(reaction_dir))
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.queue_id, entry.queue_id)

    def test_get_active_entry_for_reaction_dir_ignores_terminal_entry(self) -> None:
        reaction_dir = self.root / "terminal_lookup"
        entry = enqueue(self.root, str(reaction_dir))
        mark_completed(self.root, entry.queue_id)
        found = get_active_entry_for_reaction_dir(self.root, str(reaction_dir))
        self.assertIsNone(found)

    def test_reconcile_orphaned_running_ignores_root_report_only(self) -> None:
        reaction_dir = self.root / "mol_done"
        reaction_dir.mkdir()
        entry = enqueue(self.root, str(reaction_dir))
        dequeue_next(self.root)

        report_json_path(reaction_dir).write_text(
            json.dumps(
                orca_artifact_payload(
                    job_id=entry.task_id,
                    run_id="run_done_1",
                    reaction_dir=str(reaction_dir),
                    status="completed",
                    final_result={
                        "status": "completed",
                        "completed_at": "2026-03-10T04:59:59+00:00",
                    },
                )
            ),
            encoding="utf-8",
        )

        changed = reconcile_orphaned_running_entries(self.root)
        self.assertEqual(changed, 1)

        entries = list_queue(self.root)

        found = next(item for item in entries if item.queue_id == entry.queue_id)
        self.assertEqual(found.status, QueueStatus.PENDING)
        self.assertIsNone(queue_entry_run_id(found))
        self.assertEqual(found.finished_at, "")

    def test_reconcile_orphaned_force_entry_ignores_previous_generation_state(self) -> None:
        reaction_dir = self.root / "mol_force_state"
        reaction_dir.mkdir()
        previous = new_state(reaction_dir, reaction_dir / "job.inp", max_retries=0)
        previous["job_id"] = "task-a"
        finalize_state(
            reaction_dir,
            previous,
            status="completed",
            final_result={
                "status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-03-10T04:59:59+00:00",
            },
        )
        current = enqueue(
            self.root,
            str(reaction_dir),
            force=True,
            task_id="task-b",
        )
        dequeue_next(self.root)

        changed = reconcile_orphaned_running_entries(self.root)

        self.assertEqual(changed, 1)
        found = self._find_entry(current.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.PENDING)
        self.assertIsNone(queue_entry_run_id(found))

    def test_reconcile_orphaned_force_same_task_requires_run_identity(self) -> None:
        reaction_dir = self.root / "mol_force_same_task"
        reaction_dir.mkdir()
        previous = new_state(reaction_dir, reaction_dir / "old.inp", max_retries=0)
        previous["job_id"] = "task-same"
        previous_run_id = previous["run_id"]
        finalize_state(
            reaction_dir,
            previous,
            status="completed",
            final_result={
                "status": "completed",
                "reason": "previous_generation",
                "completed_at": "2026-03-10T04:59:59+00:00",
            },
        )
        previous_entry = enqueue(
            self.root,
            str(reaction_dir),
            task_id="task-same",
        )
        dequeue_next(self.root)
        mark_completed(self.root, previous_entry.queue_id, run_id=previous_run_id)
        self._finish_terminal_replay(previous_entry.queue_id)
        current = enqueue(
            self.root,
            str(reaction_dir),
            force=True,
            task_id="task-same",
        )
        dequeue_next(self.root)

        changed = reconcile_orphaned_running_entries(self.root)

        self.assertEqual(changed, 1)
        found = self._find_entry(current.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.PENDING)
        self.assertIsNone(queue_entry_run_id(found))
        persisted = load_state(reaction_dir)
        assert persisted is not None
        self.assertEqual(persisted["run_id"], previous_run_id)
        self.assertEqual(persisted["status"], "completed")

    def test_reconcile_orphaned_force_same_task_accepts_new_run_identity(self) -> None:
        reaction_dir = self.root / "mol_force_same_task_current"
        reaction_dir.mkdir()
        previous = new_state(reaction_dir, reaction_dir / "old.inp", max_retries=0)
        previous["job_id"] = "task-same"
        finalize_state(
            reaction_dir,
            previous,
            status="completed",
            final_result={
                "status": "completed",
                "reason": "previous_generation",
                "completed_at": "2026-03-10T04:59:59+00:00",
            },
        )
        previous_entry = enqueue(
            self.root,
            str(reaction_dir),
            task_id="task-same",
        )
        dequeue_next(self.root)
        mark_completed(self.root, previous_entry.queue_id, run_id=previous["run_id"])
        self._finish_terminal_replay(previous_entry.queue_id)
        current = enqueue(
            self.root,
            str(reaction_dir),
            force=True,
            task_id="task-same",
        )
        dequeue_next(self.root)
        current_state = new_state(
            reaction_dir,
            reaction_dir / "current.inp",
            max_retries=0,
        )
        current_state["job_id"] = "task-same"
        finalize_state(
            reaction_dir,
            current_state,
            status="completed",
            final_result={
                "status": "completed",
                "reason": "current_generation",
                "completed_at": "2026-03-11T04:59:59+00:00",
            },
        )

        changed = reconcile_orphaned_running_entries(self.root)

        self.assertEqual(changed, 1)
        found = self._find_entry(current.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.COMPLETED)
        self.assertEqual(queue_entry_run_id(found), current_state["run_id"])

    def test_reconcile_orphaned_force_same_task_rejects_prior_report_run(self) -> None:
        reaction_dir = self.root / "mol_force_same_task_report"
        reaction_dir.mkdir()
        previous_run_id = "run-previous"
        previous_entry = enqueue(
            self.root,
            str(reaction_dir),
            task_id="task-same",
        )
        dequeue_next(self.root)
        mark_completed(self.root, previous_entry.queue_id, run_id=previous_run_id)
        self._finish_terminal_replay(previous_entry.queue_id)
        report_json_path(reaction_dir).write_text(
            json.dumps(
                orca_artifact_payload(
                    job_id="task-same",
                    run_id=previous_run_id,
                    reaction_dir=str(reaction_dir),
                    status="completed",
                    final_result={
                        "status": "completed",
                        "completed_at": "2026-03-10T04:59:59+00:00",
                    },
                )
            ),
            encoding="utf-8",
        )
        current = enqueue(
            self.root,
            str(reaction_dir),
            force=True,
            task_id="task-same",
        )
        dequeue_next(self.root)

        changed = reconcile_orphaned_running_entries(self.root)

        self.assertEqual(changed, 1)
        found = self._find_entry(current.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.PENDING)
        self.assertIsNone(queue_entry_run_id(found))

    def test_reconcile_orphaned_force_entry_ignores_previous_generation_report(self) -> None:
        reaction_dir = self.root / "mol_force_report"
        reaction_dir.mkdir()
        report_json_path(reaction_dir).write_text(
            json.dumps(
                orca_artifact_payload(
                    job_id="task-a",
                    run_id="run-a",
                    reaction_dir=str(reaction_dir),
                    status="completed",
                    final_result={
                        "status": "completed",
                        "completed_at": "2026-03-10T04:59:59+00:00",
                    },
                )
            ),
            encoding="utf-8",
        )
        current = enqueue(
            self.root,
            str(reaction_dir),
            force=True,
            task_id="task-b",
        )
        dequeue_next(self.root)

        changed = reconcile_orphaned_running_entries(self.root)

        self.assertEqual(changed, 1)
        found = self._find_entry(current.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.PENDING)
        self.assertIsNone(queue_entry_run_id(found))

    def test_orca_engine_dequeue_skips_foreign_engine_entries(self) -> None:
        # The ORCA worker shares the runs root with standalone xTB/CREST jobs.
        # Its configured dequeue must skip a foreign-engine entry even on the
        # single-root fast path, mirroring the crest/xtb app filter (#29/#30);
        # otherwise the ORCA worker claims and mis-runs a CREST/xTB job.
        orca_entry = enqueue(self.root, str(self.root / "orca_job"))
        foreign = replace(
            orca_entry,
            queue_id="q_crest_1",
            app_name="orca_auto_crest",
            engine="crest",
            priority=1,  # higher priority than the ORCA entry -> claimed first if unfiltered
            metadata={**orca_entry.metadata, "reaction_dir": str(self.root / "crest_job")},
        )
        queue_store.save_entries(self.root, [foreign, orca_entry])

        queue_functions = ENGINE_DEFINITION.queue_functions
        assert queue_functions is not None
        dequeue = queue_functions.dequeue_next

        claimed = dequeue(self.root)
        assert claimed is not None
        self.assertEqual(claimed.queue_id, orca_entry.queue_id)
        # The foreign CREST entry is left unclaimed by the ORCA worker.
        self.assertIsNone(dequeue(self.root))

    def test_reconcile_honors_cancel_requested_orphan(self) -> None:
        # A running job that was cancel-requested and then lost its worker (no
        # run.lock, no terminal state, no job_report) must be reconciled to a
        # terminal CANCELLED state, not re-queued to PENDING where dequeue would
        # skip it forever (cancel_requested entries are never dequeued).
        reaction_dir = self.root / "mol_cancel"
        reaction_dir.mkdir()
        entry = enqueue(self.root, str(reaction_dir))
        dequeue_next(self.root)  # -> RUNNING
        cancel(self.root, entry.queue_id)  # cancel_requested=True, stays RUNNING

        running = self._find_entry(entry.queue_id)
        assert running is not None
        self.assertEqual(running.status, QueueStatus.RUNNING)
        self.assertTrue(running.cancel_requested)

        changed = reconcile_orphaned_running_entries(self.root)
        self.assertEqual(changed, 1)

        found = self._find_entry(entry.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.CANCELLED)
        self.assertFalse(found.cancel_requested)
        self.assertTrue(found.finished_at)

    def test_reconcile_skips_when_worker_pid_is_alive(self) -> None:
        reaction_dir = self.root / "mol_done"
        reaction_dir.mkdir()
        entry = enqueue(self.root, str(reaction_dir))
        dequeue_next(self.root)

        report_json_path(reaction_dir).write_text(
            json.dumps(
                orca_artifact_payload(
                    job_id="run_done_1",
                    run_id="run_done_1",
                    reaction_dir=str(reaction_dir),
                    status="completed",
                    final_result={
                        "status": "completed",
                        "completed_at": "2026-03-10T04:59:59+00:00",
                    },
                )
            ),
            encoding="utf-8",
        )
        (self.root / "queue_worker.pid").write_text(str(os.getpid()), encoding="utf-8")

        changed = reconcile_orphaned_running_entries(self.root)

        self.assertEqual(changed, 0)
        found = self._find_entry(entry.queue_id)
        assert found is not None
        self.assertEqual(found.status, QueueStatus.RUNNING)

    # -- queue lookup via list ------------------------------------------

    def test_lookup_entry_exists(self) -> None:
        entry = enqueue(self.root, str(self.root / "mol_A"))
        found = self._find_entry(entry.queue_id)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.queue_id, entry.queue_id)

    def test_lookup_entry_missing(self) -> None:
        self.assertIsNone(self._find_entry("q_nonexistent"))

    # -- priority tie-breaking by arrival (queue-file row) order --------

    def test_fifo_on_same_priority(self) -> None:
        e1 = enqueue(self.root, str(self.root / "first"))
        enqueue(self.root, str(self.root / "second"))
        dequeued = dequeue_next(self.root)
        assert dequeued is not None
        self.assertEqual(dequeued.queue_id, e1.queue_id)
