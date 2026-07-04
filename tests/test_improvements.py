"""Tests for retry expansion, crash recovery, and structured logging."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.core.admission import reserve_slot
from orca_auto.orca.commands.run_inp import _cmd_run_inp_execute
from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.orca_runner import RunResult
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.state import load_state, save_state
from orca_auto.orca.state_machine import (
    RESUMABLE_FAILED_REASONS,
    decide_attempt_outcome,
    is_resumable_state,
)
from orca_auto.orca.statuses import AnalyzerStatus
from orca_auto.orca.types import RunState

# ── Retry Strategy Expansion ──


class TestMemoryErrorDetection(unittest.TestCase):
    def _analyze(self, text: str) -> AnalyzerStatus:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "calc.out"
            out.write_text(text, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        return result.status

    def test_out_of_memory(self) -> None:
        self.assertEqual(self._analyze("OUT OF MEMORY\n"), AnalyzerStatus.ERROR_MEMORY)

    def test_insufficient_memory(self) -> None:
        self.assertEqual(self._analyze("INSUFFICIENT MEMORY\n"), AnalyzerStatus.ERROR_MEMORY)

    def test_cannot_allocate_memory(self) -> None:
        self.assertEqual(self._analyze("CANNOT ALLOCATE MEMORY\n"), AnalyzerStatus.ERROR_MEMORY)

    def test_memory_takes_priority_over_scf(self) -> None:
        text = "OUT OF MEMORY\nSCF NOT CONVERGED\n"
        self.assertEqual(self._analyze(text), AnalyzerStatus.ERROR_MEMORY)


class TestGeomNotConvergedDetection(unittest.TestCase):
    def _analyze(self, text: str) -> AnalyzerStatus:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "calc.out"
            out.write_text(text, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        return result.status

    def test_optimization_did_not_converge(self) -> None:
        self.assertEqual(
            self._analyze("THE OPTIMIZATION DID NOT CONVERGE\n"),
            AnalyzerStatus.GEOM_NOT_CONVERGED,
        )

    def test_optimization_not_yet_converged(self) -> None:
        self.assertEqual(
            self._analyze("OPTIMIZATION HAS NOT YET CONVERGED\n"),
            AnalyzerStatus.GEOM_NOT_CONVERGED,
        )

    def test_geom_not_converged_if_terminated_normally_still_completed(self) -> None:
        # If ORCA terminated normally despite geom warning, treat as completed
        text = "OPTIMIZATION HAS NOT YET CONVERGED\n****ORCA TERMINATED NORMALLY****\n"
        self.assertEqual(self._analyze(text), AnalyzerStatus.COMPLETED)


class TestDecideAttemptOutcomeExpanded(unittest.TestCase):
    def test_memory_error_is_retryable(self) -> None:
        result = decide_attempt_outcome(
            analyzer_status=AnalyzerStatus.ERROR_MEMORY,
            analyzer_reason="out_of_memory",
            retries_used=0,
            max_retries=4,
        )
        self.assertIsNone(result)  # None means "keep retrying"

    def test_geom_not_converged_is_retryable(self) -> None:
        result = decide_attempt_outcome(
            analyzer_status=AnalyzerStatus.GEOM_NOT_CONVERGED,
            analyzer_reason="geometry_not_converged",
            retries_used=0,
            max_retries=4,
        )
        self.assertIsNone(result)


# ── Crash Recovery ──


class TestCrashRecovery(unittest.TestCase):
    def _write_config(self, root: Path, allowed_root: Path) -> Path:
        fake_orca = root / "fake_orca"
        fake_orca.touch()
        fake_orca.chmod(0o755)
        config = root / "orca_auto.yaml"
        config.write_text(
            json.dumps(
                {
                    "orca": {
                        "runtime": {
                            "allowed_root": str(allowed_root),
                            "default_max_retries": 4,
                        },
                        "paths": {"orca_executable": str(fake_orca)},
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_crashed_recovery_is_resumable_reason(self) -> None:
        self.assertIn("crashed_recovery", RESUMABLE_FAILED_REASONS)

    def test_worker_shutdown_is_resumable_reason(self) -> None:
        self.assertIn("worker_shutdown", RESUMABLE_FAILED_REASONS)

    def test_crashed_state_is_resumable(self) -> None:
        state: RunState = {
            "status": "failed",
            "final_result": {"reason": "crashed_recovery"},
        }
        self.assertTrue(is_resumable_state(state))

    def test_worker_shutdown_state_is_resumable(self) -> None:
        state: RunState = {
            "status": "failed",
            "final_result": {"reason": "worker_shutdown"},
        }
        self.assertTrue(is_resumable_state(state))

    def test_crash_recovery_resumes_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_crash"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")

            # Simulate a crashed state: status=running, no lock file
            state: RunState = {
                "run_id": "run_crashed",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 4,
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(reaction / "rxn.out"),
                        "return_code": 1,
                        "analyzer_status": "error_scf",
                        "analyzer_reason": "scf_not_converged",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    }
                ],
                "final_result": None,
            }
            save_state(reaction, state)
            # No run.lock file → crashed

            def _fake_run(_self, inp_path: Path) -> RunResult:
                out = inp_path.with_suffix(".out")
                out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=0)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                token = reserve_slot(
                    reaction.parent / ".admission",
                    1,
                    work_dir=str(reaction),
                    source="queue_worker",
                    state="reserved",
                )
                rc = _cmd_run_inp_execute(
                    type(
                        "Args",
                        (),
                        {
                            "config": str(config),
                            "reaction_dir": str(reaction),
                            "force": False,
                        },
                    )(),
                    reservation_token=token,
                )
            saved = load_state(reaction)

        self.assertEqual(rc, 0)
        assert saved is not None
        self.assertEqual(saved["run_id"], "run_crashed")  # Preserved run_id
        self.assertEqual(saved["status"], "completed")
        final_result = saved["final_result"]
        assert final_result is not None
        self.assertTrue(final_result["resumed"])


class TestCLILogFileFlag(unittest.TestCase):
    def test_log_file_flag_is_accepted(self) -> None:
        from orca_auto.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["scan-notify", "--log-file", "/tmp/test.log"])
        self.assertEqual(args.log_file, "/tmp/test.log")
