import json
import os
import tempfile
import unittest
from pathlib import Path

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue.adapter import (
    DuplicateEntryError,
    cancel,
    clear_terminal,
    dequeue_next,
    enqueue,
    get_active_entry_for_reaction_dir,
    get_cancel_requested,
    has_pending_entries,
    list_queue,
    mark_completed,
    mark_failed,
    queue_entry_force,
    queue_entry_reaction_dir,
    queue_entry_run_id,
    reconcile_orphaned_running_entries,
)
from orca_auto.orca.state import report_json_path
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
            "Use --force to re-enqueue a completed/failed job, or cancel the existing entry first.",
        )

    def test_duplicate_running_entry_blocked(self) -> None:
        enqueue(self.root, str(self.root / "mol_A"))
        dequeue_next(self.root)  # → running
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, str(self.root / "mol_A"))

    def test_duplicate_terminal_without_force_blocked(self) -> None:
        """Completed/failed entries block re-enqueue without --force (accidental)."""
        entry = enqueue(self.root, str(self.root / "mol_A"))
        mark_completed(self.root, entry.queue_id)
        with self.assertRaises(DuplicateEntryError):
            enqueue(self.root, str(self.root / "mol_A"))

    def test_duplicate_terminal_with_force_allowed(self) -> None:
        """Completed/failed entries allow re-enqueue with --force (intentional retry)."""
        entry = enqueue(self.root, str(self.root / "mol_A"))
        mark_completed(self.root, entry.queue_id)
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
        removed = clear_terminal(self.root)
        self.assertEqual(removed, 2)
        remaining = list_queue(self.root)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].status, QueueStatus.PENDING)

    def test_list_queue_can_count_running(self) -> None:
        enqueue(self.root, str(self.root / "a"))
        enqueue(self.root, str(self.root / "b"))
        dequeue_next(self.root)
        dequeue_next(self.root)
        running = [entry for entry in list_queue(self.root) if entry.status == QueueStatus.RUNNING]
        self.assertEqual(len(running), 2)

    def test_has_pending_entries_false_when_empty(self) -> None:
        self.assertFalse(has_pending_entries(self.root))

    def test_has_pending_entries_true_when_pending_exists(self) -> None:
        enqueue(self.root, str(self.root / "pending"))
        self.assertTrue(has_pending_entries(self.root))

    def test_has_pending_entries_false_when_only_running_exists(self) -> None:
        enqueue(self.root, str(self.root / "running_only"))
        dequeue_next(self.root)
        self.assertFalse(has_pending_entries(self.root))

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

    def test_reconcile_orphaned_running_entry_from_job_report(self) -> None:
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

        changed = reconcile_orphaned_running_entries(self.root)
        self.assertEqual(changed, 1)

        entries = list_queue(self.root)

        found = next(item for item in entries if item.queue_id == entry.queue_id)
        self.assertEqual(found.status, QueueStatus.COMPLETED)
        self.assertEqual(queue_entry_run_id(found), "run_done_1")
        self.assertEqual(found.finished_at, "2026-03-10T04:59:59+00:00")

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

    # -- priority tie-breaking by enqueued_at ---------------------------

    def test_fifo_on_same_priority(self) -> None:
        e1 = enqueue(self.root, str(self.root / "first"))
        enqueue(self.root, str(self.root / "second"))
        dequeued = dequeue_next(self.root)
        assert dequeued is not None
        self.assertEqual(dequeued.queue_id, e1.queue_id)
