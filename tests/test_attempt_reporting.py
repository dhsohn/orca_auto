from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orca_auto.core.engine_runner import executable_identity
from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca.attempt.reporting import (
    build_final_result,
    exit_with_result,
    finished_notification_already_sent,
    last_out_path_from_state,
)
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import RunFinishedNotification


class TestAttemptReporting(unittest.TestCase):
    def test_finished_notification_marker_reads_canonical_state(self) -> None:
        self.assertTrue(
            finished_notification_already_sent(
                {"final_result": {"finished_notification_sent_at": "2026-07-11T00:00:00Z"}}
            )
        )
        self.assertFalse(finished_notification_already_sent({"final_result": {}}))

    def test_last_out_path_from_state_defensive_cases(self) -> None:
        self.assertIsNone(last_out_path_from_state({"attempts": []}))
        self.assertIsNone(last_out_path_from_state({"attempts": ["bad"]}))
        self.assertIsNone(last_out_path_from_state({"attempts": [{"out_path": "   "}]}))
        self.assertEqual(
            last_out_path_from_state({"attempts": [{"out_path": "/tmp/run.out"}]}),
            "/tmp/run.out",
        )

    def test_last_out_path_prefers_newer_interrupted_retry_publication(self) -> None:
        state = {
            "attempts": [{"index": 1, "out_path": "/tmp/run.out"}],
            "scratch_publications": [
                {
                    "attempt_index": 2,
                    "inp_path": "/tmp/run.retry01.inp",
                    "publication": {"published_files": ["run.retry01.gbw", "run.retry01.out"]},
                }
            ],
        }

        self.assertEqual(last_out_path_from_state(state), "/tmp/run.retry01.out")

    def test_build_final_result_keeps_supported_extra_fields_only(self) -> None:
        result = build_final_result(
            status=RunStatus.FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="runner_failed",
            last_out_path="/tmp/run.out",
            resumed=False,
            extra={
                "skipped_execution": True,
                "runner_error": "boom",
                "ignored": 123,
            },
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["analyzer_status"], "incomplete")
        self.assertTrue(result["skipped_execution"])
        self.assertEqual(result["runner_error"], "boom")
        self.assertNotIn("ignored", result)

    def test_exit_with_result_writes_state_reports_and_finished_notification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            generation = reaction_dir / "20260714-224054-959479f2"
            generation.mkdir()
            selected_inp = generation / "rxn.inp"
            selected_inp.write_text("! Opt\n", encoding="utf-8")
            generation_status = generation.stat()
            reaction_status = reaction_dir.stat()
            owner_token = "attempt-report-owner-token-0001"
            bind_direct_generation_owner(
                reaction_dir,
                namespace=generation.name,
                expected_job_identity=(reaction_status.st_dev, reaction_status.st_ino),
                expected_generation_identity=(
                    generation_status.st_dev,
                    generation_status.st_ino,
                ),
                owner_token=owner_token,
            )
            state = new_state(reaction_dir, selected_inp)
            state["execution_provenance"] = {
                "execution_dir": str(generation),
                "execution_dir_identity": {
                    "device": generation_status.st_dev,
                    "inode": generation_status.st_ino,
                },
                "generation_owner_token": owner_token,
                "bound_selected_identity": executable_identity(selected_inp),
            }
            emitted_payloads: list[dict] = []
            finished_notifications: list[RunFinishedNotification] = []

            def notify_finished(payload: RunFinishedNotification) -> bool:
                finished_notifications.append(payload)
                return True

            rc = exit_with_result(
                reaction_dir,
                state,
                selected_inp,
                status=RunStatus.COMPLETED,
                analyzer_status=AnalyzerStatus.COMPLETED,
                reason="normal_termination",
                last_out_path=str(reaction_dir / "rxn.out"),
                resumed=True,
                exit_code=0,
                emit=lambda payload: emitted_payloads.append(payload),
                extra={"skipped_execution": True},
                notify_finished=notify_finished,
            )

            saved = load_state(reaction_dir)
            machine = json.loads((generation / "machine.json").read_text(encoding="utf-8"))
            self.assertFalse((reaction_dir / "machine.json").exists())
            expected_report_json = str(generation / "machine.json")

        self.assertEqual(rc, 0)
        assert saved is not None
        self.assertEqual(saved["status"], "completed")
        assert saved["final_result"] is not None
        self.assertEqual(saved["final_result"]["reason"], "normal_termination")
        self.assertEqual(saved["final_result"]["last_out_path"], str(reaction_dir / "rxn.out"))
        self.assertEqual(len(emitted_payloads), 1)
        self.assertEqual(emitted_payloads[0]["status"], "completed")
        self.assertEqual(emitted_payloads[0]["run_state"], str(reaction_dir / "job_state.json"))
        self.assertEqual(emitted_payloads[0]["report_json"], expected_report_json)
        self.assertEqual(machine["lifecycle"]["outcome"], "succeeded")
        self.assertEqual(machine["payload"]["data"]["summary"]["status"], "completed")
        self.assertIn("finished_notification_sent_at", saved["final_result"])
        self.assertEqual(len(finished_notifications), 1)
        self.assertTrue(finished_notifications[0]["resumed"])
        self.assertTrue(finished_notifications[0]["skipped_execution"])
