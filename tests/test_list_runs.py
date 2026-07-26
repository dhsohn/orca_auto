"""Unified list command tests."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.cli import main
from orca_auto.core.admission import activate_reserved_slot, reserve_slot
from orca_auto.orca.queue.adapter import (
    dequeue_next,
    enqueue,
    mark_completed,
    update_metadata,
)
from orca_auto.orca.runtime.run_lock import acquire_run_lock
from orca_auto.orca.state import report_json_path, save_state, state_path
from tests.engine_artifact_helpers import orca_artifact_payload


class _ListTestBase(unittest.TestCase):
    """Shared helpers for list tests."""

    def _write_config(self, root: Path, allowed_root: Path) -> Path:
        fake_orca = root / "fake_orca"
        fake_orca.touch()
        fake_orca.chmod(0o755)
        config = root / "orca_auto.yaml"
        config.write_text(
            json.dumps(
                {
                    "runs_root": str(allowed_root),
                    "orca": {
                        "runtime": {
                            "default_max_retries": 2,
                        },
                        "paths": {"orca_executable": str(fake_orca)},
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def _activate_admission_slot(self, allowed_root: Path, reaction_dir: Path) -> None:
        """Mirror a live run: an active slot in <runs root>/.admission."""
        admission_root = allowed_root / ".admission"
        token = reserve_slot(
            admission_root,
            4,
            work_dir=str(reaction_dir),
            source="queue_worker",
            state="reserved",
        )
        assert token is not None
        activate_reserved_slot(admission_root, token)

    def _make_run(
        self,
        reaction_dir: Path,
        *,
        status: str = "completed",
        started_at: str = "2026-03-01T00:00:00+00:00",
        updated_at: str = "2026-03-01T01:00:00+00:00",
        inp_name: str = "rxn.inp",
        run_id: str | None = None,
    ) -> None:
        reaction_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "run_id": run_id or f"run_{reaction_dir.name}",
            "reaction_dir": str(reaction_dir),
            "selected_inp": str(reaction_dir / inp_name),
            "max_retries": 2,
            "status": status,
            "started_at": started_at,
            "updated_at": updated_at,
            "attempts": [{"index": 1}],
            "final_result": {"status": status},
        }
        save_state(reaction_dir, state)
        if status in {"running", "retrying"}:
            # A genuinely in-progress run holds a live run lock; without it the
            # activity list now treats the run as a stale/failed leftover.
            self.enterContext(acquire_run_lock(reaction_dir))


class TestListEmpty(_ListTestBase):
    def test_list_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("active_simulations: 0", output)
        self.assertNotIn("- ", output)


class TestListStandaloneRuns(_ListTestBase):
    """Test listing standalone runs (not queued)."""

    def test_shows_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            self._make_run(allowed / "rxn1", status="completed")
            self._make_run(
                allowed / "rxn2", status="running", started_at="2026-03-02T00:00:00+00:00"
            )
            self._activate_admission_slot(allowed, allowed / "rxn2")
            config = self._write_config(root, allowed)

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("rxn1", output)
        self.assertIn("rxn2", output)
        self.assertIn("✅", output)
        self.assertIn("▶", output)
        self.assertIn("active_simulations: 1", output)

    def test_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            self._make_run(allowed / "rxn1", status="completed")
            self._make_run(
                allowed / "rxn2", status="running", started_at="2026-03-02T00:00:00+00:00"
            )
            self._activate_admission_slot(allowed, allowed / "rxn2")
            config = self._write_config(root, allowed)

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    [
                        "queue",
                        "list",
                        "--config",
                        str(config),
                        "--engine",
                        "orca",
                        "--kind",
                        "job",
                        "--status",
                        "running",
                    ]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("rxn2", output)
        self.assertNotIn("rxn1", output)
        self.assertIn("active_simulations: 1", output)

    def test_nested_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            self._make_run(allowed / "project" / "rxn1", status="completed")
            self._make_run(allowed / "project" / "rxn2", status="failed")
            config = self._write_config(root, allowed)

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("rxn1", output)
        self.assertIn("rxn2", output)
        self.assertIn("active_simulations: 0", output)

    def test_tracked_organized_run_is_listed_via_job_locations_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            organized = root / "organized" / "project" / "rxn_tracked"
            allowed.mkdir()
            organized.mkdir(parents=True)
            config = self._write_config(root, allowed)

            state = {
                "run_id": "run_tracked",
                "reaction_dir": str(organized),
                "selected_inp": str(organized / "tracked.inp"),
                "max_retries": 2,
                "status": "completed",
                "started_at": "2026-03-01T00:00:00+00:00",
                "updated_at": "2026-03-01T01:00:00+00:00",
                "attempts": [{"index": 1}],
                "final_result": {"status": "completed"},
            }
            save_state(organized, state)
            (allowed / "job_locations.json").write_text(
                json.dumps(
                    [
                        {
                            "job_id": "job_tracked",
                            "app_name": "orca_auto_orca",
                            "job_type": "orca_opt",
                            "status": "completed",
                            "original_run_dir": str(allowed / "project" / "rxn_tracked"),
                            "molecule_key": "rxn_tracked",
                            "selected_input_xyz": str(organized / "tracked.inp"),
                            "latest_known_path": str(organized),
                            "resource_request": {},
                            "resource_actual": {},
                        }
                    ],
                    ensure_ascii=True,
                    indent=2,
                ),
                encoding="utf-8",
            )

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("run_tracked", output)
        self.assertIn("✅", output)
        self.assertIn("active_simulations: 0", output)


class TestListQueueEntries(_ListTestBase):
    """Test listing queue entries in unified view."""

    def test_queue_entries_shown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            rxn_dir = allowed / "mol_A"
            rxn_dir.mkdir()
            entry = enqueue(allowed, str(rxn_dir))

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn("active_simulations: 0", output)
        self.assertIn(entry.queue_id, output)
        self.assertIn("ORCA", output)
        self.assertIn("⏳", output)

    def test_filter_pending(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            rxn_a = allowed / "mol_A"
            rxn_a.mkdir()
            entry = enqueue(allowed, str(rxn_a))
            # Also add a standalone completed run
            self._make_run(allowed / "rxn_done", status="completed")

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    [
                        "queue",
                        "list",
                        "--config",
                        str(config),
                        "--engine",
                        "orca",
                        "--kind",
                        "job",
                        "--status",
                        "pending",
                    ]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn(entry.queue_id, output)
        self.assertIn("ORCA", output)
        self.assertNotIn("rxn_done", output)

    def test_queue_with_run_state(self) -> None:
        """Queue entry enriched with run_state data."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            rxn_dir = allowed / "mol_A"
            rxn_dir.mkdir()
            entry = enqueue(allowed, str(rxn_dir))
            # Create a run_state for the same directory
            self._make_run(
                rxn_dir,
                status="running",
                started_at="2026-03-02T00:00:00+00:00",
                inp_name="opt.inp",
            )

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn(entry.queue_id, output)
        self.assertIn("active_simulations: 0", output)
        self.assertIn("ORCA", output)
        self.assertIn("⏳", output)

    def test_list_does_not_terminalize_orphaned_entry_from_root_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            rxn_dir = allowed / "mol_done"
            rxn_dir.mkdir()
            entry = enqueue(allowed, str(rxn_dir))
            dequeue_next(allowed)
            report_json_path(rxn_dir).write_text(
                json.dumps(
                    orca_artifact_payload(
                        job_id=entry.task_id,
                        run_id="run_done_1",
                        reaction_dir=str(rxn_dir),
                        status="completed",
                        final_result={
                            "status": "completed",
                            "completed_at": "2026-03-10T04:59:59+00:00",
                        },
                    )
                ),
                encoding="utf-8",
            )

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(
                    ["queue", "list", "--config", str(config), "--engine", "orca", "--kind", "job"]
                )

        self.assertEqual(rc, 0)
        output = captured.getvalue()
        self.assertIn(entry.queue_id, output)
        self.assertIn("⏳", output)
        self.assertNotIn("✅", output)
        self.assertNotIn("▶", output)


class TestListClear(_ListTestBase):
    """Test list clear subaction."""

    def test_clear_queue_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            rxn_dir = allowed / "mol_A"
            rxn_dir.mkdir()
            entry = enqueue(allowed, str(rxn_dir))
            mark_completed(allowed, entry.queue_id)
            # Mirror the worker after terminal side effects are durably published.
            self.assertTrue(
                update_metadata(
                    allowed,
                    entry.queue_id,
                    {"orca_terminal_replay": None},
                )
            )

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(["queue", "list", "--config", str(config), "clear"])

        self.assertEqual(rc, 0)
        self.assertIn("Cleared", captured.getvalue())

    def test_clear_standalone_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            self._make_run(allowed / "rxn1", status="completed")
            self._make_run(
                allowed / "rxn2", status="running", started_at="2026-03-02T00:00:00+00:00"
            )
            config = self._write_config(root, allowed)

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(["queue", "list", "--config", str(config), "clear"])

            self.assertEqual(rc, 0)
            # rxn1 (completed) should be cleared
            self.assertFalse(state_path(allowed / "rxn1").exists())
            # rxn2 (running) should remain
            self.assertTrue(state_path(allowed / "rxn2").exists())

    def test_clear_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            allowed.mkdir()
            config = self._write_config(root, allowed)

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                rc = main(["queue", "list", "--config", str(config), "clear"])

        self.assertEqual(rc, 0)
        self.assertIn("Nothing to clear.", captured.getvalue())
