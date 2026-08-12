import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
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
    write_state,
)
from orca_auto.orca.types import RunFinalResult


def _validate_common_machine(path: Path) -> None:
    validator = os.environ.get("FACTORY_MACHINE_CONTRACT_VALIDATOR")
    if validator:
        subprocess.run([sys.executable, validator, "--machine", str(path)], check=True)


def _bind_generation(reaction: Path, *, token: str) -> tuple[Path, dict]:
    """Create a verified execution generation and the matching state fields."""
    generation = reaction / "20260714-224054-959479f2"
    generation.mkdir()
    inp = generation / "nebts.inp"
    inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    generation_status = generation.stat()
    reaction_status = reaction.stat()
    bind_direct_generation_owner(
        reaction,
        namespace=generation.name,
        expected_job_identity=(reaction_status.st_dev, reaction_status.st_ino),
        expected_generation_identity=(generation_status.st_dev, generation_status.st_ino),
        owner_token=token,
    )
    provenance = {
        "execution_dir": str(generation),
        "execution_dir_identity": {
            "device": generation_status.st_dev,
            "inode": generation_status.st_ino,
        },
        "generation_owner_token": token,
        "bound_selected_identity": executable_identity(inp),
    }
    return generation, provenance


class TestState(unittest.TestCase):
    def test_generation_state_read_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            generation = Path(td) / "20260714-224054-959479f2"
            generation.mkdir()
            (generation / "job_state.json").write_text("{}" * 8, encoding="utf-8")

            with patch.object(state_module, "MAX_RUN_ARTIFACT_JSON_BYTES", 8):
                self.assertIsNone(state_module.load_generation_state(generation))

    def test_generation_report_read_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation, provenance = _bind_generation(reaction, token="bounded-report-token-0001")
            state = new_state(reaction, generation / "nebts.inp", max_retries=0)
            state["execution_provenance"] = provenance
            report_path = write_report_json(reaction, dict(state))
            assert report_path is not None

            with patch.object(
                state_module,
                "MAX_RUN_ARTIFACT_JSON_BYTES",
                report_path.stat().st_size - 1,
            ):
                self.assertIsNone(load_report_json(generation))

    def test_generation_report_rejects_swap_after_confined_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation, provenance = _bind_generation(reaction, token="swapped-report-token-0001")
            state = new_state(reaction, generation / "nebts.inp", max_retries=0)
            state["execution_provenance"] = provenance
            report_path = write_report_json(reaction, dict(state))
            assert report_path is not None
            original_target = state_module._visible_generation_artifact_dir

            def replace_after_read(
                reaction_dir: Path,
                payload: dict,
            ) -> tuple[Path, tuple[int, int]] | None:
                target = original_target(reaction_dir, payload)
                report_path.write_text(
                    report_path.read_text(encoding="utf-8") + "\n",
                    encoding="utf-8",
                )
                return target

            with patch.object(
                state_module,
                "_visible_generation_artifact_dir",
                side_effect=replace_after_read,
            ):
                self.assertIsNone(load_report_json(generation))

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
            self.assertTrue(lock_path.exists())

    def test_active_lock_blocks_second_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            with acquire_run_lock(reaction):
                with self.assertRaises(RuntimeError):
                    with acquire_run_lock(reaction):
                        pass

    def test_unlocked_stale_metadata_is_reused_without_pid_probe(self) -> None:
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

            with patch(
                "orca_auto.orca.runtime.run_lock.current_process_lock_payload",
                return_value={
                    "pid": os.getpid(),
                    "started_at": "2026-03-22T00:00:00+00:00",
                    "process_start_ticks": 333,
                },
            ):
                with acquire_run_lock(reaction):
                    payload = json.loads(lock_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload.get("pid"), os.getpid())
                    self.assertEqual(payload.get("process_start_ticks"), 333)

    def test_state_and_reports_are_written_without_tmp_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation, provenance = _bind_generation(reaction, token="state-leak-token-0001")
            inp = generation / "nebts.inp"
            state = new_state(reaction, inp, max_retries=1)
            self.assertRegex(str(state["run_id"]), re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{32}$"))
            state["execution_provenance"] = provenance

            save_state(reaction, state)
            loaded = load_state(reaction)
            self.assertIsInstance(loaded, dict)

            write_report_files(reaction, state)
            self.assertTrue(state_module.report_json_path(generation).exists())
            self.assertFalse((reaction / "job_report.json").exists())

            self.assertEqual(list(reaction.glob("*.tmp.*")), [])
            self.assertEqual(list(generation.glob("*.tmp.*")), [])

    def test_queue_identity_survives_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=1)
            state["queue_id"] = "q-orca-round-trip"
            state["queue_generation"] = "a" * 64

            save_state(reaction, state)
            loaded = load_state(reaction)
            assert loaded is not None
            self.assertEqual(loaded["queue_id"], "q-orca-round-trip")
            self.assertEqual(loaded["queue_generation"], "a" * 64)

            save_state(reaction, loaded)
            raw = json.loads((reaction / "job_state.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["job"]["queue_id"], "q-orca-round-trip")
            self.assertEqual(raw["job"]["generation"], "a" * 64)

    def test_direct_state_keeps_queue_identity_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            save_state(reaction, new_state(reaction, inp, max_retries=1))

            raw = json.loads((reaction / "job_state.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["job"]["queue_id"], "")
            self.assertEqual(raw["job"]["generation"], "")

    def test_write_report_files_skips_publication_without_verified_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=1)

            reports = write_report_files(reaction, state)

            self.assertEqual(reports, {})
            self.assertFalse((reaction / "job_report.json").exists())

    def test_write_state_fails_closed_when_pinned_reaction_directory_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "reaction"
            displaced = root / "reaction-displaced"
            reaction.mkdir()
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            state = new_state(reaction, inp, max_retries=1)
            original_identity = (reaction.stat().st_dev, reaction.stat().st_ino)

            @contextmanager
            def _replace_after_pin(
                directory_fd: int,
                lock_name: str,
                *,
                display_path: Path | None = None,
                timeout_seconds: float = 10.0,
            ):
                del lock_name, display_path, timeout_seconds
                pinned = os.fstat(directory_fd)
                self.assertEqual((pinned.st_dev, pinned.st_ino), original_identity)
                reaction.rename(displaced)
                reaction.mkdir()
                yield

            with (
                patch.object(state_module, "file_lock_at", _replace_after_pin),
                self.assertRaisesRegex(ValueError, "parent directory identity changed"),
            ):
                write_state(reaction, state)

            self.assertFalse((reaction / "job_state.json").exists())
            self.assertFalse((displaced / "job_state.json").exists())
            self.assertEqual(list(reaction.glob(".job_state.json.*.tmp")), [])

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
            self.assertFalse((reaction / ".orca_auto_orca_executions").exists())
            self.assertFalse((reaction / ".orca_auto_input_snapshots").exists())

    def test_generation_report_leaves_root_copies_untouched(self) -> None:
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
            state_module.report_json_path(reaction).write_text("{}", encoding="utf-8")

            write_report_files(reaction, state)

            # Unbound root files are left untouched; the writer publishes only
            # into the verified generation.
            self.assertTrue(state_module.report_json_path(reaction).exists())
            self.assertTrue(state_module.report_json_path(generation).is_file())

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
            self.assertFalse((reaction / "job_report.json").exists())
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
            generation, provenance = _bind_generation(reaction, token="state-helper-token-0001")
            inp = generation / "nebts.inp"
            state = new_state(reaction, inp, max_retries=2)
            state["execution_provenance"] = provenance

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
                "execution_provenance": provenance,
                "final_result": None,
            }
            self.assertEqual(
                write_report_json(reaction, report_payload),
                state_module.report_json_path(generation),
            )
            written_report = load_report_json(generation)
            assert written_report is not None
            self.assertEqual(written_report["engine"], "orca")
            self.assertEqual(written_report["engine_payload"]["run_id"], state["run_id"])
            self.assertEqual(written_report["status"]["state"], "created")

    def test_write_report_files_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation, provenance = _bind_generation(reaction, token="state-fields-token-0001")
            inp = generation / "nebts.inp"
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
                **provenance,
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
            write_state(reaction, state)
            result = write_report_files(reaction, state)
            report_json_path = Path(result["report_json"])

            observation = json.loads(report_json_path.read_text(encoding="utf-8"))
            _validate_common_machine(report_json_path)
            self.assertEqual(
                observation["contract"],
                {"name": "factory/machine-observation", "version": 1},
            )
            self.assertEqual(observation["operation"]["kind"], "chemistry/orca-run")
            self.assertEqual(observation["lifecycle"]["outcome"], "succeeded")
            self.assertEqual(observation["handoff"]["status"], "blocked")
            self.assertEqual(observation["delivery"]["status"], "incomplete")
            report = load_report_json(generation)
            assert report is not None
            self.assertIsNone(load_report_json(generation, require_consumable_success=True))
            self.assertEqual(report["status"]["state"], "completed")
            self.assertEqual(report["engine_payload"]["max_retries"], 3)
            self.assertEqual(len(report["engine_payload"]["attempts"]), 1)
            self.assertEqual(report["engine_payload"]["attempts"], state["attempts"])
            self.assertEqual(
                report["engine_payload"]["execution_provenance"],
                state["execution_provenance"],
            )
            self.assertIsNotNone(report["engine_payload"]["final_result"])

    def test_write_report_files_reentry_preserves_published_terminal_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation, provenance = _bind_generation(reaction, token="reentry-terminal-tok-01")
            inp = generation / "nebts.inp"
            state = new_state(reaction, inp, max_retries=0)
            state["status"] = "completed"
            state["execution_provenance"] = provenance
            state["final_result"] = {
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-01-01T00:00:00+00:00",
                "last_out_path": str(reaction / "rxn.out"),
            }
            write_state(reaction, state)
            first = write_report_files(reaction, state)
            report_json_path = Path(first["report_json"])
            published = {
                path.name: path.read_bytes()
                for path in report_json_path.parent.iterdir()
                if path.is_file()
            }

            changed = dict(state)
            changed["updated_at"] = "2026-02-02T00:00:00+00:00"
            second = write_report_files(reaction, changed)

            self.assertEqual(second["report_json"], first["report_json"])
            for name, data in published.items():
                self.assertEqual(
                    (report_json_path.parent / name).read_bytes(),
                    data,
                    f"re-entry modified published artifact {name}",
                )

    def test_terminal_machine_observation_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            generation, provenance = _bind_generation(
                reaction,
                token="immutable-machine-token-0001",
            )
            state = new_state(reaction, generation / "nebts.inp", max_retries=0)
            state["status"] = "failed"
            state["execution_provenance"] = provenance
            final_result: RunFinalResult = {
                "status": "failed",
                "analyzer_status": "incomplete",
                "reason": "runner_exception",
            }
            state["final_result"] = final_result
            write_state(reaction, state)

            path = write_report_json(reaction, dict(state))
            assert path is not None
            original_identity = (path.stat().st_dev, path.stat().st_ino)

            self.assertEqual(write_report_json(reaction, dict(state)), path)
            self.assertEqual((path.stat().st_dev, path.stat().st_ino), original_identity)

            changed = dict(state)
            changed["final_result"] = {
                **final_result,
                "reason": "cancel_requested",
            }
            with self.assertRaisesRegex(RuntimeError, "terminal machine observation is immutable"):
                write_report_json(reaction, changed)

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
            with acquire_run_lock(reaction):
                self.assertTrue(lock_path.exists())

    def test_unlocked_invalid_lock_payload_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            lock_path = reaction / "run.lock"
            lock_path.write_text(
                json.dumps({"pid": "invalid", "started_at": "x"}) + "\n", encoding="utf-8"
            )
            with acquire_run_lock(reaction):
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())
