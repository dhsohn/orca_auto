from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orca_auto.orca.attempt.engine import run_attempts
from orca_auto.orca.orca_runner import WorkerShutdownInterrupt
from orca_auto.orca.retry_policy import (
    effective_max_retries,
    retry_policy_for_input,
    retry_recipe_name_for_input,
)
from orca_auto.orca.state import load_state, new_state


class _InterruptRunner:
    def run(self, _inp_path: Path):
        raise KeyboardInterrupt


class _WorkerShutdownRunner:
    def run(self, _inp_path: Path):
        raise WorkerShutdownInterrupt


class _RetryThenSuccessRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, inp_path: Path):
        self.calls += 1
        inp_path.with_suffix(".xyz").write_text(
            "2\nretry geometry\nH 0 0 0\nH 0 0 0.75\n",
            encoding="utf-8",
        )
        out_path = inp_path.with_suffix(".out")
        if self.calls == 1:
            out_path.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
            return SimpleNamespace(out_path=str(out_path), return_code=1)
        out_path.write_text(
            "****ORCA TERMINATED NORMALLY****\nTOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 0 msec\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _AlwaysScfFailRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        inp_path.with_suffix(".xyz").write_text(
            "2\nretry geometry\nH 0 0 0\nH 0 0 0.75\n",
            encoding="utf-8",
        )
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
        return SimpleNamespace(out_path=str(out_path), return_code=1)


class _NoArtifactScfFailRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
        return SimpleNamespace(out_path=str(out_path), return_code=1)


class _OptTsRetryThenSuccessRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        inp_path.with_suffix(".xyz").write_text(
            "2\nTS retry geometry\nH 0 0 0\nH 0 0 0.75\n",
            encoding="utf-8",
        )
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) == 1:
            out_path.write_text(
                "\n".join(
                    [
                        "VIBRATIONAL FREQUENCIES",
                        "  1    120.00 cm**-1",
                        "  2    240.00 cm**-1",
                        "****ORCA TERMINATED NORMALLY****",
                    ]
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _OptTsRetryTwiceThenSuccessRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        inp_path.with_suffix(".xyz").write_text(
            "2\nTS retry geometry\nH 0 0 0\nH 0 0 0.75\n",
            encoding="utf-8",
        )
        out_path = inp_path.with_suffix(".out")
        if len(self.seen) <= 2:
            out_path.write_text(
                "\n".join(
                    [
                        "VIBRATIONAL FREQUENCIES",
                        "  1    120.00 cm**-1",
                        "  2    240.00 cm**-1",
                        "****ORCA TERMINATED NORMALLY****",
                    ]
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(out_path=str(out_path), return_code=0)
        out_path.write_text(
            "\n".join(
                [
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -150.00 cm**-1",
                    "  2    120.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


class _UnusedRunner:
    def run(self, _inp_path: Path):
        raise AssertionError("runner.run() should not be called for terminal resumed attempts")


class _CaptureSuccessRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(
            "****ORCA TERMINATED NORMALLY****\nTOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 0 msec\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)


def _retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    return selected_inp.parent / f"{selected_inp.stem}.retry{retry_number:02d}.inp"


class TestAttemptEngine(unittest.TestCase):
    def test_keyboard_interrupt_emits_single_run_interrupted_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=3)

            emitted_payloads = []

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=_InterruptRunner(),
                max_retries=3,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda payload: emitted_payloads.append(payload),
            )

            saved = load_state(reaction_dir)

        self.assertEqual(rc, 130)
        self.assertIsNotNone(saved)
        assert saved is not None
        final_result = saved["final_result"]
        assert final_result is not None
        self.assertEqual(final_result["reason"], "interrupted_by_user")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(len(emitted_payloads), 1)

    def test_worker_shutdown_propagates_without_failed_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=3)

            emitted_payloads = []

            with self.assertRaises(WorkerShutdownInterrupt):
                run_attempts(
                    reaction_dir,
                    selected_inp,
                    state,
                    resumed=False,
                    runner=_WorkerShutdownRunner(),
                    max_retries=3,
                    retry_inp_path=_retry_inp_path,
                    to_resolved_local=lambda raw: Path(raw),
                    emit=lambda payload: emitted_payloads.append(payload),
                )

            saved = load_state(reaction_dir)

        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertIsNone(saved["final_result"])
        self.assertEqual(saved["status"], "running")
        self.assertEqual(emitted_payloads, [])

    def test_standalone_optts_policy_disables_retry_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text(
                "! OptTS B3LYP def2-SVP Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )
            state = new_state(reaction_dir, selected_inp, max_retries=2)
            notifications = []

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=_OptTsRetryThenSuccessRunner(),
                max_retries=2,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
                notify_retry=lambda payload: notifications.append(payload),
            )
            retry_inp = reaction_dir / "rxn.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(notifications, [])
        self.assertFalse(retry_inp.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["max_retries"], 0)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "retry_limit_reached")

    def test_opt_policy_disables_artifact_restart_despite_configured_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=8)
            runner = _AlwaysScfFailRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                max_retries=8,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            retry_inp = reaction_dir / "rxn.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry_inp.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["max_retries"], 0)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "retry_limit_reached")

    def test_no_retry_policy_ignores_missing_artifacts_for_non_scants_routes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=8)
            runner = _NoArtifactScfFailRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                max_retries=8,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            retry_inp = reaction_dir / "rxn.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry_inp.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["max_retries"], 0)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "retry_limit_reached")

    def test_standalone_optts_policy_disables_retry_despite_configured_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "ts.inp"
            selected_inp.write_text(
                "! OptTS B3LYP def2-SVP Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
            state = new_state(reaction_dir, selected_inp, max_retries=8)
            runner = _OptTsRetryThenSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                max_retries=8,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            retry01 = reaction_dir / "ts.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry01.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["max_retries"], 0)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "retry_limit_reached")

    def test_neb_ts_policy_disables_retry_despite_configured_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "neb.inp"
            selected_inp.write_text(
                "! NEB-TS B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
            state = new_state(reaction_dir, selected_inp, max_retries=8)
            runner = _OptTsRetryThenSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                max_retries=8,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            retry01 = reaction_dir / "neb.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry01.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["max_retries"], 0)

    def test_start_and_finish_callbacks_emit_immediate_terminal_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp, max_retries=2)
            started_notifications = []
            finished_notifications = []
            retry_notifications = []

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=_CaptureSuccessRunner(),
                max_retries=2,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
                notify_started=lambda payload: started_notifications.append(payload),
                notify_finished=lambda payload: finished_notifications.append(payload),
                notify_retry=lambda payload: retry_notifications.append(payload),
            )

        self.assertEqual(rc, 0)
        self.assertEqual(len(started_notifications), 1)
        self.assertEqual(retry_notifications, [])
        self.assertEqual(len(finished_notifications), 1)

        started = started_notifications[0]
        self.assertEqual(started["attempt_index"], 1)
        self.assertEqual(started["status"], "running")
        self.assertTrue(started["current_inp"].endswith("rxn.inp"))

        finished = finished_notifications[0]
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["analyzer_status"], "completed")
        self.assertEqual(finished["reason"], "normal_termination")
        self.assertEqual(finished["attempt_count"], 1)
        last_out_path = finished["last_out_path"]
        assert last_out_path is not None
        self.assertTrue(last_out_path.endswith("rxn.out"))

    def test_resumed_terminal_attempt_finishes_without_running_again(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            out_path = reaction_dir / "rxn.out"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            out_path.write_text(
                "****ORCA TERMINATED NORMALLY****\nTOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 0 msec\n",
                encoding="utf-8",
            )
            state = new_state(reaction_dir, selected_inp, max_retries=2)
            state["attempts"].append(
                {
                    "index": 1,
                    "inp_path": str(selected_inp),
                    "out_path": str(out_path),
                    "return_code": 0,
                    "analyzer_status": "completed",
                    "analyzer_reason": "normal_termination",
                    "markers": {},
                    "patch_actions": [],
                    "started_at": "2026-03-22T00:00:00+00:00",
                    "ended_at": "2026-03-22T00:00:01+00:00",
                }
            )
            finished_notifications = []
            emitted_payloads = []

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=True,
                runner=_UnusedRunner(),
                max_retries=2,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda payload: emitted_payloads.append(payload),
                notify_finished=lambda payload: finished_notifications.append(payload),
            )

        self.assertEqual(rc, 0)
        self.assertEqual(len(emitted_payloads), 1)
        self.assertEqual(len(finished_notifications), 1)
        self.assertEqual(finished_notifications[0]["status"], "completed")
        self.assertTrue(finished_notifications[0]["resumed"])
        self.assertEqual(finished_notifications[0]["last_out_path"], str(out_path))

    def test_resumed_run_uses_gbw_checkpoint_restart_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            selected_inp.with_suffix(".gbw").write_bytes(b"checkpoint")
            selected_inp.with_suffix(".xyz").write_text(
                "2\nresume geometry\nH 0 0 0\nH 0 0 0.75\n",
                encoding="utf-8",
            )
            state = new_state(reaction_dir, selected_inp, max_retries=2)
            runner = _CaptureSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=True,
                runner=runner,
                max_retries=2,
                retry_inp_path=_retry_inp_path,
                to_resolved_local=lambda raw: Path(raw),
                emit=lambda _payload: None,
            )

            saved = load_state(reaction_dir)
            resume_inp = reaction_dir / "rxn.resume.inp"
            resume_text = resume_inp.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(runner.seen, [resume_inp])
        self.assertIn('%moinp "rxn.gbw"', resume_text)
        self.assertIn("MORead", resume_text)
        self.assertEqual(saved["attempts"][0]["inp_path"], str(resume_inp))
        self.assertIn(
            "resume_checkpoint_restart_from_rxn.gbw", saved["attempts"][0]["patch_actions"]
        )
        self.assertIn("resume_geometry_restart_from_rxn.xyz", saved["attempts"][0]["patch_actions"])


class TestRetryPolicy(unittest.TestCase):
    def test_policy_retry_counts_are_calculation_type_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opt = root / "opt.inp"
            opt.write_text("! Opt B3LYP def2-SVP\n", encoding="utf-8")
            optts = root / "optts.inp"
            optts.write_text("! OptTS B3LYP def2-SVP Freq\n", encoding="utf-8")
            scants = root / "scants.inp"
            scants.write_text("! ScanTS B3LYP def2-SVP Freq\n", encoding="utf-8")
            freq = root / "freq.inp"
            freq.write_text("! Freq B3LYP def2-SVP\n", encoding="utf-8")

            self.assertEqual(retry_policy_for_input(opt).name, "opt")
            self.assertEqual(effective_max_retries(opt, configured_max_retries=8), 0)
            self.assertEqual(retry_policy_for_input(optts).name, "standalone_ts")
            self.assertEqual(effective_max_retries(optts, configured_max_retries=8), 0)
            self.assertEqual(retry_policy_for_input(scants).name, "scants")
            self.assertEqual(effective_max_retries(scants, configured_max_retries=8), 3)
            self.assertEqual(retry_policy_for_input(freq).name, "freq")
            self.assertEqual(effective_max_retries(freq, configured_max_retries=8), 0)
            self.assertEqual(effective_max_retries(scants, configured_max_retries=0), 0)

    def test_policy_recipes_keep_generic_hardening_off_non_scants_routes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opt = root / "opt.inp"
            opt.write_text("! Opt B3LYP def2-SVP\n", encoding="utf-8")
            optts = root / "optts.inp"
            optts.write_text("! OptTS B3LYP def2-SVP Freq\n", encoding="utf-8")
            scants = root / "scants.inp"
            scants.write_text("! ScanTS B3LYP def2-SVP Freq\n", encoding="utf-8")

            self.assertEqual(retry_recipe_name_for_input(scants, 2), "scants_retry")
            self.assertEqual(retry_recipe_name_for_input(opt, 1), "no_route_rewrite")
            self.assertEqual(retry_recipe_name_for_input(optts, 1), "no_route_rewrite")

    def test_policy_reads_split_simple_route_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scants = root / "split_scants.inp"
            scants.write_text(
                "! B3LYP def2-SVP\n! ScanTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )
            optts = root / "split_optts.inp"
            optts.write_text(
                "! B3LYP def2-SVP\n! OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )

            self.assertEqual(retry_policy_for_input(scants).name, "scants")
            self.assertEqual(effective_max_retries(scants, configured_max_retries=8), 3)
            self.assertEqual(retry_policy_for_input(optts).name, "standalone_ts")
            self.assertEqual(effective_max_retries(optts, configured_max_retries=8), 0)
