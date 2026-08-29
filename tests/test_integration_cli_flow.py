import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.queue.adapter import (
    list_queue,
    queue_entry_force,
    queue_entry_reaction_dir,
)
from orca_auto.orca.state_reading import state_path


def _run_main(argv: list[str]) -> int:
    from orca_auto.cli import main

    return main(argv)


def _write_config(
    root: Path,
    allowed_root: Path,
    orca_executable: Path,
    *,
    max_concurrent: int = 4,
) -> Path:
    config = root / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {
                "runs_root": str(allowed_root),
                "scheduler": {
                    "max_active_simulations": max_concurrent,
                },
                "orca": {
                    "runtime": {
                        "default_max_retries": 2,
                    },
                    "paths": {"orca_executable": str(orca_executable)},
                },
            }
        ),
        encoding="utf-8",
    )
    return config


def _write_fake_orca(binary_path: Path, counter_path: Path) -> None:
    script = f"""#!/usr/bin/env python3
from pathlib import Path

COUNTER = Path({str(counter_path)!r})
count = 0
if COUNTER.exists():
    try:
        count = int(COUNTER.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        count = 0
COUNTER.write_text(str(count + 1), encoding="utf-8")
print("****ORCA TERMINATED NORMALLY****")
raise SystemExit(0)
"""
    binary_path.write_text(script, encoding="utf-8")
    binary_path.chmod(0o755)


class TestIntegrationCliFlow(unittest.TestCase):
    def test_run_inp_submit_only_enqueues_without_executing_orca(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            reaction = allowed / "project_a" / "rxn_queue_demo"
            reaction.mkdir(parents=True)

            counter = root / "fake_orca_counter.txt"
            fake_orca = root / "fake_orca.py"
            _write_fake_orca(fake_orca, counter)
            config = _write_config(root, allowed, fake_orca)

            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

            run_stdout = io.StringIO()
            with patch("sys.stdout", run_stdout):
                rc = _run_main(
                    [
                        "run-dir",
                        str(reaction),
                        "--config",
                        str(config),
                    ]
                )

            queue_entries = [
                entry
                for entry in list_queue(allowed)
                if queue_entry_reaction_dir(entry) == str(reaction.resolve())
            ]

        self.assertEqual(rc, 0)
        self.assertIn("status: queued", run_stdout.getvalue())
        self.assertFalse(counter.exists())
        self.assertEqual(len(queue_entries), 1)
        self.assertEqual(queue_entries[0].status, QueueStatus.PENDING)
        self.assertFalse(state_path(reaction).exists())

    def test_force_submit_preserves_force_flag_in_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            reaction = allowed / "rxn_force_queue"
            reaction.mkdir(parents=True)

            counter = root / "fake_orca_counter.txt"
            fake_orca = root / "fake_orca.py"
            _write_fake_orca(fake_orca, counter)
            config = _write_config(root, allowed, fake_orca)

            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

            run_stdout = io.StringIO()
            with patch("sys.stdout", run_stdout):
                rc = _run_main(
                    [
                        "run-dir",
                        str(reaction),
                        "--config",
                        str(config),
                        "--force",
                    ]
                )

            queue_entries = [
                entry
                for entry in list_queue(allowed)
                if queue_entry_reaction_dir(entry) == str(reaction.resolve())
            ]

        self.assertEqual(rc, 0)
        self.assertIn("status: queued", run_stdout.getvalue())
        self.assertEqual(len(queue_entries), 1)
        self.assertTrue(queue_entry_force(queue_entries[0]))
        self.assertFalse(counter.exists())

    def test_existing_completed_output_is_queued_for_worker_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "orca_runs"
            reaction = allowed / "rxn_completed_skip"
            reaction.mkdir(parents=True)

            counter = root / "fake_orca_counter.txt"
            fake_orca = root / "fake_orca.py"
            _write_fake_orca(fake_orca, counter)
            config = _write_config(root, allowed, fake_orca)

            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            (reaction / "rxn.out").write_text(
                "****ORCA TERMINATED NORMALLY****\n", encoding="utf-8"
            )

            rc = _run_main(
                [
                    "run-dir",
                    str(reaction),
                    "--config",
                    str(config),
                ]
            )
            queue_entries = [
                entry
                for entry in list_queue(allowed)
                if queue_entry_reaction_dir(entry) == str(reaction.resolve())
            ]

        self.assertEqual(rc, 0)
        self.assertFalse(counter.exists())
        self.assertEqual(len(queue_entries), 1)
        self.assertFalse(state_path(reaction).exists())
