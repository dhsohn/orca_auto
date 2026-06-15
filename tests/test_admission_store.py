import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.admission import (
    AdmissionStoreCorruptError,
    activate_reserved_slot,
    active_slot_count,
    list_slots,
    reconcile_stale_slots,
    release_slot,
    reserve_slot,
)
from orca_auto.core.admission import (
    update_slot_metadata as admission_update_slot_metadata,
)
from orca_auto.core.admission.store import ADMISSION_FILE_NAME


class TestAdmissionStore(unittest.TestCase):
    def test_invalid_admission_store_fails_closed(self) -> None:
        for bad_payload in ("{not json", json.dumps({"token": "oops"})):
            with self.subTest(bad_payload=bad_payload), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / ADMISSION_FILE_NAME
                path.write_text(bad_payload, encoding="utf-8")

                with self.assertRaises(AdmissionStoreCorruptError):
                    list_slots(root)
                with self.assertRaises(AdmissionStoreCorruptError):
                    reserve_slot(root, 1, source="queue_worker")
                self.assertEqual(path.read_text(encoding="utf-8"), bad_payload)

    def test_reserved_slot_activates_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "queued"
            reaction_dir.mkdir()

            token = reserve_slot(
                root,
                1,
                queue_id="q_test",
                source="queue_worker",
                state="reserved",
            )
            self.assertIsNotNone(token)
            self.assertEqual(active_slot_count(root), 1)

            activated = activate_reserved_slot(
                root,
                token or "",
                work_dir=reaction_dir,
                source="queue_run",
                queue_id="q_test",
            )
            self.assertIsNotNone(activated)
            slots = list_slots(root)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].state, "active")
            self.assertEqual(slots[0].work_dir, str(reaction_dir))
            self.assertEqual(slots[0].queue_id, "q_test")
            self.assertEqual(slots[0].source, "queue_run")

            release_slot(root, token or "")
            self.assertEqual(active_slot_count(root), 0)

    def test_reserved_slot_activation_can_attach_app_and_task_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "queued_meta"
            reaction_dir.mkdir()

            token = reserve_slot(
                root,
                1,
                queue_id="q_meta",
                source="queue_worker",
                state="reserved",
            )
            self.assertIsNotNone(token)

            activated = activate_reserved_slot(
                root,
                token or "",
                work_dir=reaction_dir,
                source="queue_run",
                queue_id="q_meta",
                app_name="orca_auto_orca",
                task_id="task_meta_123",
            )
            self.assertIsNotNone(activated)
            slots = list_slots(root)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].app_name, "orca_auto_orca")
            self.assertEqual(slots[0].task_id, "task_meta_123")
            self.assertEqual(slots[0].work_dir, str(reaction_dir))

            release_slot(root, token or "")
            self.assertEqual(active_slot_count(root), 0)

    def test_reserve_slot_is_reserved_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = reserve_slot(root, 1, source="queue_worker", state="reserved")

            self.assertIsNotNone(token)
            slots = list_slots(root)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].token, token)
            self.assertEqual(slots[0].state, "reserved")

    def test_reserve_slot_uses_slot_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            token = reserve_slot(root, 1, source="queue_worker", state="reserved")

            self.assertIsNotNone(token)
            self.assertTrue((token or "").startswith("slot_"))
            self.assertEqual(active_slot_count(root), 1)

    def test_reconcile_stale_slots_removes_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = [
                {
                    "token": "slot_dead",
                    "state": "active",
                    "work_dir": str(root / "rxn"),
                    "queue_id": None,
                    "owner_pid": 987654321,
                    "process_start_ticks": None,
                    "source": "direct_run",
                    "acquired_at": "2026-03-20T00:00:00+00:00",
                }
            ]
            (root / ADMISSION_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

            removed = reconcile_stale_slots(root)

            self.assertEqual(removed, 1)
            self.assertEqual(active_slot_count(root), 0)

    @patch("orca_auto.core.admission.store._process_start_ticks", return_value=999)
    @patch("orca_auto.core.admission.store.os.kill", return_value=None)
    def test_list_slots_treats_pid_reuse_as_stale(
        self,
        mock_alive,
        mock_ticks,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = [
                {
                    "token": "slot_reused",
                    "state": "active",
                    "work_dir": str(root / "rxn"),
                    "queue_id": None,
                    "owner_pid": os.getpid(),
                    "process_start_ticks": 123,
                    "source": "direct_run",
                    "acquired_at": "2026-03-20T00:00:00+00:00",
                }
            ]
            (root / ADMISSION_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

            slots = list_slots(root)

            self.assertEqual(slots, [])
            mock_alive.assert_called()
            mock_ticks.assert_called()

    @patch("orca_auto.core.admission.store.os.kill", return_value=None)
    def test_list_slots_normalizes_work_dir_payload(self, mock_alive) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "rxn"
            payload = [
                {
                    "token": "slot_work_dir",
                    "state": "active",
                    "work_dir": str(reaction_dir),
                    "queue_id": "",
                    "owner_pid": os.getpid(),
                    "process_start_ticks": None,
                    "source": "queue_run",
                    "acquired_at": "2026-03-20T00:00:00+00:00",
                    "app_name": "orca_auto_orca",
                    "task_id": "task_123",
                }
            ]
            (root / ADMISSION_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

            slots = list_slots(root)

            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].work_dir, str(reaction_dir))
            self.assertEqual(slots[0].task_id, "task_123")
            mock_alive.assert_called()

    def test_update_slot_metadata_populates_reserved_slot_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = reserve_slot(root, 1, source="queue_worker", state="reserved")
            self.assertIsNotNone(token)

            updated = admission_update_slot_metadata(
                root,
                token or "",
                state="active",
                queue_id="q_123",
                app_name="orca_auto_orca",
                task_id="orca_task_123",
                workflow_id="wf_123",
                work_dir=root / "rxn",
                owner_pid=os.getpid(),
            )

            self.assertTrue(updated)
            slots = list_slots(root)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].state, "active")
            self.assertEqual(slots[0].queue_id, "q_123")
            self.assertEqual(slots[0].app_name, "orca_auto_orca")
            self.assertEqual(slots[0].task_id, "orca_task_123")
            self.assertEqual(slots[0].workflow_id, "wf_123")
            self.assertEqual(slots[0].work_dir, str(root / "rxn"))
            self.assertEqual(slots[0].owner_pid, os.getpid())

    def test_local_store_reports_missing_activation_release_and_metadata_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = reserve_slot(root, 1, source="queue_worker")
            self.assertIsNotNone(token)

            self.assertFalse(
                activate_reserved_slot(
                    root,
                    "slot_missing",
                    work_dir=root / "rxn",
                    source="queue_run",
                )
            )
            self.assertFalse(release_slot(root, "slot_missing"))
            self.assertFalse(
                admission_update_slot_metadata(root, "slot_missing", queue_id="q_missing")
            )
            activated = activate_reserved_slot(
                root,
                token or "",
                work_dir="   ",
                source="queue_run",
            )
            self.assertIsNotNone(activated)
            self.assertEqual(activated.work_dir if activated is not None else None, "")

    def test_reserve_slot_with_explicit_owner_pid_records_observed_start_ticks(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "orca_auto.core.admission.store._process_start_ticks",
                return_value=777,
            ) as mock_ticks,
        ):
            root = Path(tmp)
            token = reserve_slot(root, 1, source="queue_worker", owner_pid=12345)

            payload = json.loads((root / ADMISSION_FILE_NAME).read_text(encoding="utf-8"))

        self.assertIsNotNone(token)
        self.assertEqual(payload[0]["owner_pid"], 12345)
        self.assertEqual(payload[0]["process_start_ticks"], 777)
        mock_ticks.assert_called_once_with(12345)
