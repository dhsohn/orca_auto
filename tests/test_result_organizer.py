from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from orca_auto.orca.result_organizer_filesystem import check_conflict
from orca_auto.orca.result_organizer_planning import (
    check_eligibility,
    compute_organize_plan,
    detect_job_type,
    plan_root_scan,
)
from orca_auto.orca.state import load_state, save_state, state_path, write_report_files
from orca_auto.orca.types import RunState


def _write_state(reaction_dir: Path, state: Mapping[str, object]) -> None:
    save_state(reaction_dir, dict(state))


def _write_invalid_state(reaction_dir: Path, text: str) -> None:
    state_path(reaction_dir).write_text(text, encoding="utf-8")


def _make_completed_dir(root: Path, name: str, route: str = "! Opt") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    inp = d / "rxn.inp"
    inp.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    out = d / "rxn.out"
    out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    state: RunState = {
        "run_id": f"run_20260222_101530_{name[:8].ljust(8, '0')}",
        "reaction_dir": str(d),
        "selected_inp": str(inp),
        "status": "completed",
        "started_at": "2026-02-22T10:15:30+00:00",
        "updated_at": "2026-02-22T10:15:45+00:00",
        "max_retries": 5,
        "attempts": [{"index": 1, "inp_path": str(inp), "out_path": str(out)}],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "2026-02-22T10:15:45+00:00",
            "last_out_path": str(out),
        },
    }
    _write_state(d, state)
    write_report_files(d, state)
    return d


class TestCheckEligibility(unittest.TestCase):
    def test_completed_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1")
            state, skip = check_eligibility(d)
            self.assertIsNotNone(state)
            self.assertIsNone(skip)

    def test_failed_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            _write_state(d, {"run_id": "run_test", "status": "failed", "final_result": {}})
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            self.assertIsNotNone(skip)
            assert skip is not None
            self.assertEqual(skip.reason, "not_completed")

    def test_created_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            _write_state(d, {"run_id": "run_test", "status": "created"})
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "not_completed")

    def test_running_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            _write_state(d, {"run_id": "run_test", "status": "running"})
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "not_completed")

    def test_missing_state_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "state_missing_or_invalid")

    def test_invalid_json_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            _write_invalid_state(d, "not json")
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "state_missing_or_invalid")

    def test_missing_run_id_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            _write_state(d, {"status": "completed", "final_result": {}})
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "state_schema_invalid")

    def test_completed_missing_final_result_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            _write_state(d, {"run_id": "run_test", "status": "completed"})
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "final_result_missing")

    def test_completed_missing_out_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            inp = d / "rxn.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            _write_state(
                d,
                {
                    "run_id": "run_test",
                    "status": "completed",
                    "selected_inp": str(inp),
                    "final_result": {
                        "status": "completed",
                        "last_out_path": str(d / "nonexistent.out"),
                    },
                },
            )
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "state_output_mismatch")

    def test_missing_run_state_is_skipped_even_when_report_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rxn1"
            d.mkdir()
            (d / "job_report.json").write_text(
                json.dumps({"run_id": "run_test", "status": "completed"}),
                encoding="utf-8",
            )
            state, skip = check_eligibility(d)
            self.assertIsNone(state)
            assert skip is not None
            self.assertEqual(skip.reason, "state_missing_or_invalid")


class TestDetectJobType(unittest.TestCase):
    def _inp(self, td: str, route: str) -> Path:
        p = Path(td) / "rxn.inp"
        p.write_text(f"{route}\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
        return p

    def test_optts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! OptTS Freq")), "ts")

    def test_neb_ts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! NEB-TS")), "ts")

    def test_opt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! Opt Freq")), "opt")

    def test_sp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! SP def2-SVP")), "sp")

    def test_energy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! Energy")), "sp")

    def test_freq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! Freq")), "freq")

    def test_numfreq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! NumFreq")), "freq")

    def test_anfreq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! AnFreq")), "freq")

    def test_other(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! B3LYP def2-SVP")), "other")

    def test_optts_not_classified_as_opt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_job_type(self._inp(td, "! OptTS IRC")), "ts")


class TestComputeOrganizePlan(unittest.TestCase):
    def test_correct_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1", route="! OptTS Freq")
            state = load_state(d)
            assert state is not None
            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            self.assertEqual(plan.job_type, "ts")
            self.assertIn("ts/", plan.target_rel_path)
            self.assertIn(state["run_id"], plan.target_rel_path)

    def test_extracts_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1")
            state = load_state(d)
            assert state is not None
            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            self.assertEqual(plan.analyzer_status, "completed")
            self.assertEqual(plan.reason, "normal_termination")
            self.assertEqual(plan.attempt_count, 1)

    def test_uses_last_successful_attempt_when_selected_inp_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "mj3"
            d.mkdir()

            selected_inp = d / "mj3.inp"
            selected_inp.write_text("! Opt\n* xyzfile 0 1 missing.xyz\n", encoding="utf-8")
            selected_out = d / "mj3.out"
            selected_out.write_text("run incomplete\n", encoding="utf-8")

            retry_inp = d / "mj3.retry01.inp"
            retry_inp.write_text(
                "! Opt TightSCF SlowConv\n* xyz 0 1\nC 0 0 0\nH 1 0 0\nBr 2 0 0\nP 3 0 0\n*\n",
                encoding="utf-8",
            )
            retry_out = d / "mj3.retry01.out"
            retry_out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

            state: RunState = {
                "run_id": "run_retry_fallback",
                "reaction_dir": str(d),
                "selected_inp": str(selected_inp),
                "status": "completed",
                "started_at": "2026-02-22T10:15:30+00:00",
                "updated_at": "2026-02-22T10:15:45+00:00",
                "max_retries": 5,
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(selected_inp),
                        "out_path": str(selected_out),
                        "return_code": 64,
                        "analyzer_status": "incomplete",
                    },
                    {
                        "index": 2,
                        "inp_path": str(retry_inp),
                        "out_path": str(retry_out),
                        "return_code": 0,
                        "analyzer_status": "completed",
                    },
                ],
                "final_result": {
                    "status": "completed",
                    "analyzer_status": "completed",
                    "reason": "normal_termination",
                    "completed_at": "2026-02-22T10:15:45+00:00",
                    "last_out_path": str(retry_out),
                },
            }

            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            self.assertEqual(plan.job_type, "opt")
            self.assertEqual(plan.molecule_key, "CHBrP")
            self.assertEqual(plan.target_rel_path, "opt/CHBrP/run_retry_fallback")


class TestPlanRootScan(unittest.TestCase):
    def test_scans_multiple_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runs"
            root.mkdir()
            organized = Path(td) / "outputs"
            organized.mkdir()

            _make_completed_dir(root, "rxn1")
            _make_completed_dir(root, "rxn2")

            d_failed = root / "rxn3"
            d_failed.mkdir()
            _write_state(d_failed, {"run_id": "run_fail", "status": "failed", "final_result": {}})

            plans, skips = plan_root_scan(root, organized)
            self.assertEqual(len(plans), 2)
            self.assertEqual(len(skips), 1)
            self.assertEqual(skips[0].reason, "not_completed")

    def test_scans_nested_dirs_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runs"
            root.mkdir()
            organized = Path(td) / "outputs"
            organized.mkdir()

            _make_completed_dir(root / "batch1", "rxn_nested_a")
            _make_completed_dir(root / "batch2", "rxn_nested_b")

            plans, skips = plan_root_scan(root, organized)
            self.assertEqual(len(plans), 2)
            self.assertEqual(len(skips), 0)

    def test_scan_ignores_report_only_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runs"
            root.mkdir()
            organized = Path(td) / "outputs"
            organized.mkdir()

            # Normal completed dir
            _make_completed_dir(root, "rxn1")

            d2 = root / "rxn2"
            d2.mkdir()
            inp = d2 / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
            out = d2 / "rxn.out"
            out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
            report = {
                "run_id": "run_20260222_101530_rxn20000",
                "reaction_dir": str(d2),
                "selected_inp": str(inp),
                "status": "completed",
                "started_at": "2026-02-22T10:15:30+00:00",
                "updated_at": "2026-02-22T10:15:45+00:00",
                "max_retries": 5,
                "attempts": [{"index": 1, "inp_path": str(inp), "out_path": str(out)}],
                "final_result": {
                    "status": "completed",
                    "analyzer_status": "completed",
                    "reason": "normal_termination",
                    "completed_at": "2026-02-22T10:15:45+00:00",
                    "last_out_path": str(out),
                },
            }
            (d2 / "job_report.json").write_text(
                json.dumps(report, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            (d2 / "job_report.md").write_text("# Report\n", encoding="utf-8")

            plans, skips = plan_root_scan(root, organized)
            self.assertEqual(len(plans), 1)
            run_ids = {p.run_id for p in plans}
            self.assertNotIn("run_20260222_101530_rxn20000", run_ids)
            self.assertEqual(len(skips), 0)


class TestCheckConflict(unittest.TestCase):
    def test_no_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1")
            state = load_state(d)
            assert state is not None
            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            result = check_conflict(plan, {})
            self.assertIsNone(result)

    def test_already_organized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1")
            state = load_state(d)
            assert state is not None
            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            index = {plan.run_id: {"organized_path": plan.target_rel_path}}
            result = check_conflict(plan, index)
            self.assertEqual(result, "already_organized")

    def test_index_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1")
            state = load_state(d)
            assert state is not None
            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            index = {plan.run_id: {"organized_path": "different/path"}}
            result = check_conflict(plan, index)
            self.assertEqual(result, "index_conflict")

    def test_path_occupied(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _make_completed_dir(Path(td), "rxn1")
            state = load_state(d)
            assert state is not None
            organized = Path(td) / "outputs"
            plan = compute_organize_plan(d, state, organized)
            plan.target_abs_path.mkdir(parents=True, exist_ok=True)
            result = check_conflict(plan, {})
            self.assertEqual(result, "path_occupied")
