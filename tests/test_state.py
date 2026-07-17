import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.engine_runner import executable_identity
from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca import state as state_module
from orca_auto.orca.runtime import run_lock
from orca_auto.orca.runtime.run_lock import acquire_run_lock
from orca_auto.orca.state import (
    atomic_write_text,
    load_report_json,
    load_state,
    new_state,
    save_state,
    write_report_files,
    write_report_json,
    write_report_md,
    write_state,
)


class TestState(unittest.TestCase):
    def test_recover_stale_lock_with_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            lock_path = reaction / "run.lock"
            lock_path.write_text(
                json.dumps({"pid": 2147483647, "started_at": "2026-01-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )

            with acquire_run_lock(reaction):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload.get("pid"), os.getpid())
                self.assertIsInstance(payload.get("started_at"), str)
            self.assertFalse(lock_path.exists())

    def test_active_lock_blocks_second_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            lock_path = reaction / "run.lock"
            lock_path.write_text(
                json.dumps({"pid": os.getpid(), "started_at": "2026-01-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                with acquire_run_lock(reaction):
                    pass

    def test_active_lock_with_matching_process_ticks_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            lock_path = reaction / "run.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 12345,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "process_start_ticks": 111,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with (
                patch(
                    "orca_auto.orca.runtime.run_lock.process_lock.is_process_alive",
                    return_value=True,
                ),
                patch(
                    "orca_auto.orca.runtime.run_lock.process_lock.process_start_ticks",
                    return_value=111,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    with acquire_run_lock(reaction):
                        pass

    def test_pid_reused_lock_is_recovered_by_start_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            lock_path = reaction / "run.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 12345,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "process_start_ticks": 111,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with (
                patch(
                    "orca_auto.orca.runtime.run_lock.process_lock.is_process_alive",
                    return_value=True,
                ),
                patch(
                    "orca_auto.orca.runtime.run_lock.process_lock.process_start_ticks",
                    return_value=222,
                ),
                patch(
                    "orca_auto.orca.runtime.run_lock.current_process_lock_payload",
                    return_value={
                        "pid": os.getpid(),
                        "started_at": "2026-03-22T00:00:00+00:00",
                        "process_start_ticks": 333,
                    },
                ),
            ):
                with acquire_run_lock(reaction):
                    payload = json.loads(lock_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload.get("pid"), os.getpid())
                    self.assertEqual(payload.get("process_start_ticks"), 333)

    def test_state_and_reports_are_written_without_tmp_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=1)
            self.assertRegex(str(state["run_id"]), re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{32}$"))

            save_state(reaction, state)
            loaded = load_state(reaction)
            self.assertIsInstance(loaded, dict)

            write_report_files(reaction, state)
            report_json = reaction / "job_report.json"
            report_md = reaction / "job_report.md"
            self.assertTrue(report_json.exists())
            self.assertTrue(report_md.exists())

            tmp_files = list(reaction.glob("*.tmp.*"))
            self.assertEqual(tmp_files, [])

    def test_public_state_and_report_are_mirrored_into_visible_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation = reaction / "20260714-224054-959479f2"
            generation.mkdir()
            inp = generation / "nebts.inp"
            inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=0)
            generation_status = generation.stat()
            reaction_status = reaction.stat()
            owner_token = "state-mirror-owner-token-0001"
            bind_direct_generation_owner(
                reaction,
                namespace=generation.name,
                expected_job_identity=(reaction_status.st_dev, reaction_status.st_ino),
                expected_generation_identity=(
                    generation_status.st_dev,
                    generation_status.st_ino,
                ),
                owner_token=owner_token,
            )
            state["execution_provenance"] = {
                "execution_dir": str(generation),
                "execution_dir_identity": {
                    "device": generation_status.st_dev,
                    "inode": generation_status.st_ino,
                },
                "generation_owner_token": owner_token,
                "bound_selected_identity": executable_identity(inp),
            }

            save_state(reaction, state)
            write_report_files(reaction, state)

            # State stays mirrored (root is the live copy until terminal
            # cleanup); reports live only inside the generation.
            self.assertEqual(load_state(generation), load_state(reaction))
            self.assertIsNotNone(load_report_json(generation))
            self.assertIsNone(load_report_json(reaction))
            self.assertTrue((generation / "job_report.md").is_file())
            self.assertFalse((reaction / "job_report.md").exists())
            self.assertFalse((reaction / ".orca_auto_orca_executions").exists())
            self.assertFalse((reaction / ".orca_auto_input_snapshots").exists())

    def test_generation_report_replaces_stale_root_copies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation = reaction / "20260714-224054-959479f2"
            generation.mkdir()
            inp = generation / "nebts.inp"
            inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=0)
            generation_status = generation.stat()
            reaction_status = reaction.stat()
            owner_token = "state-mirror-owner-token-0002"
            bind_direct_generation_owner(
                reaction,
                namespace=generation.name,
                expected_job_identity=(reaction_status.st_dev, reaction_status.st_ino),
                expected_generation_identity=(
                    generation_status.st_dev,
                    generation_status.st_ino,
                ),
                owner_token=owner_token,
            )
            state["execution_provenance"] = {
                "execution_dir": str(generation),
                "execution_dir_identity": {
                    "device": generation_status.st_dev,
                    "inode": generation_status.st_ino,
                },
                "generation_owner_token": owner_token,
                "bound_selected_identity": executable_identity(inp),
            }
            (reaction / "job_report.json").write_text("{}", encoding="utf-8")
            (reaction / "job_report.md").write_text("stale", encoding="utf-8")

            write_report_files(reaction, state)

            self.assertFalse((reaction / "job_report.json").exists())
            self.assertFalse((reaction / "job_report.md").exists())
            self.assertTrue((generation / "job_report.json").is_file())
            self.assertTrue((generation / "job_report.md").is_file())

    def test_replaced_visible_generation_never_receives_state_or_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation = reaction / "20260714-224054-959479f2"
            generation.mkdir()
            inp = generation / "nebts.inp"
            inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
            generation_status = generation.stat()
            state = new_state(reaction, inp, max_retries=0)
            state["execution_provenance"] = {
                "execution_dir": str(generation),
                "execution_dir_identity": {
                    "device": generation_status.st_dev,
                    "inode": generation_status.st_ino,
                },
            }
            moved = reaction / "moved-original-generation"
            generation.rename(moved)
            generation.mkdir()
            (generation / "sentinel").write_text("replacement", encoding="utf-8")

            save_state(reaction, state)
            write_report_files(reaction, state)

            self.assertTrue((reaction / "job_state.json").is_file())
            self.assertTrue((reaction / "job_report.json").is_file())
            self.assertEqual({path.name for path in generation.iterdir()}, {"sentinel"})
            self.assertFalse((moved / "job_state.json").exists())
            self.assertFalse((moved / "job_report.json").exists())

    def test_atomic_write_text_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "sample.txt"

            atomic_write_text(target, "hello")

            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
            self.assertEqual(list(root.glob("*.tmp.*")), [])

    def test_state_module_keeps_write_helpers_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=2)

            saved_path = write_state(reaction, state)
            self.assertEqual(saved_path, state_module.state_path(reaction))
            self.assertIsNotNone(state_module.load_state(reaction))

            report_payload = {
                "run_id": state["run_id"],
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "status": "created",
                "started_at": state["started_at"],
                "updated_at": state["updated_at"],
                "attempt_count": 0,
                "max_retries": 2,
                "attempts": [],
                "final_result": None,
            }
            markdown = "# ORCA Run Report\n"

            self.assertEqual(
                write_report_json(reaction, report_payload),
                state_module.report_json_path(reaction),
            )
            self.assertEqual(
                write_report_md(reaction, markdown),
                state_module.report_md_path(reaction),
            )
            written_report = load_report_json(reaction)
            assert written_report is not None
            self.assertEqual(written_report["engine"], "orca")
            self.assertEqual(written_report["engine_payload"]["run_id"], state["run_id"])
            self.assertEqual(written_report["status"]["state"], "created")
            self.assertEqual(
                state_module.report_md_path(reaction).read_text(encoding="utf-8"), markdown
            )

    def test_write_report_files_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=3)
            state["status"] = "completed"
            state["attempts"] = [
                {
                    "index": 1,
                    "inp_path": str(inp),
                    "out_path": str(reaction / "rxn.out"),
                    "return_code": 0,
                    "analyzer_status": "completed",
                    "command": ["/opt/orca/orca", "rxn.inp"],
                    "input_identity": {
                        "path": str(inp),
                        "sha256": "a" * 64,
                        "size_bytes": 6,
                    },
                    "executable_identity": {
                        "path": "/opt/orca/orca",
                        "sha256": "b" * 64,
                        "size_bytes": 1024,
                    },
                }
            ]
            state["execution_provenance"] = {
                "bound_selected_identity": state["attempts"][0]["input_identity"],
                "materialized_inputs": {},
                "executable_identity": state["attempts"][0]["executable_identity"],
            }
            state["final_result"] = {
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-01-01T00:00:00+00:00",
                "last_out_path": str(reaction / "rxn.out"),
            }
            result = write_report_files(reaction, state)
            report_json_path = Path(result["report_json"])
            report_md_path = Path(result["report_md"])

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"]["state"], "completed")
            self.assertEqual(report["engine_payload"]["max_retries"], 3)
            self.assertEqual(len(report["engine_payload"]["attempts"]), 1)
            self.assertEqual(report["engine_payload"]["attempts"], state["attempts"])
            self.assertEqual(
                report["engine_payload"]["execution_provenance"],
                state["execution_provenance"],
            )
            self.assertIsNotNone(report["engine_payload"]["final_result"])

            md = report_md_path.read_text(encoding="utf-8")
            self.assertIn("# orca_auto ORCA Job Report", md)
            self.assertIn("## Engine Payload", md)
            self.assertIn("attempts", md)
            self.assertIn("final_result", md)
            self.assertIn("normal_termination", md)

    def test_write_report_files_does_not_publish_json_when_markdown_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=0)
            report_json = state_module.report_json_path(reaction)
            report_json.write_text('{"generation": "old"}\n', encoding="utf-8")

            with (
                patch.object(
                    state_module,
                    "write_report_md",
                    side_effect=OSError("markdown write failed"),
                ),
                patch.object(
                    state_module,
                    "write_report_json",
                    wraps=state_module.write_report_json,
                ) as json_write,
            ):
                with self.assertRaisesRegex(OSError, "markdown write failed"):
                    write_report_files(reaction, state)

            json_write.assert_not_called()
            self.assertEqual(
                report_json.read_text(encoding="utf-8"),
                '{"generation": "old"}\n',
            )

    def test_write_report_files_publishes_json_after_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=0)
            report_json = state_module.report_json_path(reaction)
            report_md = state_module.report_md_path(reaction)
            report_json.write_text('{"generation": "old"}\n', encoding="utf-8")
            report_md.write_text("# Old report\n", encoding="utf-8")
            events: list[str] = []
            original_write_report_md = state_module.write_report_md

            def write_markdown(
                target_dir: Path,
                markdown: str,
                *,
                generation_target: tuple[Path, tuple[int, int]] | None = None,
            ) -> Path:
                events.append("markdown")
                return original_write_report_md(
                    target_dir, markdown, generation_target=generation_target
                )

            def fail_json(*_args: object, **_kwargs: object) -> Path:
                events.append("json")
                raise OSError("json write failed")

            with (
                patch.object(state_module, "write_report_md", side_effect=write_markdown),
                patch.object(state_module, "write_report_json", side_effect=fail_json),
            ):
                with self.assertRaisesRegex(OSError, "json write failed"):
                    write_report_files(reaction, state)

            self.assertEqual(events, ["markdown", "json"])
            self.assertIn("# orca_auto ORCA Job Report", report_md.read_text(encoding="utf-8"))
            self.assertEqual(
                report_json.read_text(encoding="utf-8"),
                '{"generation": "old"}\n',
            )

    def test_load_report_json_returns_none_for_missing_invalid_and_non_dict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            self.assertIsNone(load_report_json(reaction))

            report_path = state_module.report_json_path(reaction)
            report_path.write_text("not valid json!!!", encoding="utf-8")
            self.assertIsNone(load_report_json(reaction))

            report_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
            self.assertIsNone(load_report_json(reaction))

    def test_load_state_returns_none_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            self.assertIsNone(load_state(reaction))

    def test_load_state_returns_none_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            state_module.state_path(reaction).write_text("not valid json!!!", encoding="utf-8")
            self.assertIsNone(load_state(reaction))

    def test_lock_released_after_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            with acquire_run_lock(reaction):
                lock_path = reaction / run_lock.LOCK_FILE_NAME
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

    def test_unreadable_lock_pid_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            lock_path = reaction / "run.lock"
            lock_path.write_text(
                json.dumps({"pid": "invalid", "started_at": "x"}) + "\n", encoding="utf-8"
            )
            with self.assertRaises(RuntimeError) as ctx:
                with acquire_run_lock(reaction):
                    pass
            self.assertIn("unreadable", str(ctx.exception).lower())
