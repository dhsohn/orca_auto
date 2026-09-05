from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orca_auto.core.engine_scratch import (
    ScratchPublication,
    attach_scratch_provenance_to_exception,
)
from orca_auto.orca.attempt.engine import run_attempts
from orca_auto.orca.orca_runner import WorkerShutdownInterrupt
from orca_auto.orca.state import new_state
from orca_auto.orca.state_reading import load_state


class _InterruptRunner:
    def run(self, _inp_path: Path):
        raise KeyboardInterrupt


class _InPlaceFailureRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def run(self, inp_path: Path):
        inp_path.with_suffix(".out").write_text("partial output\n", encoding="utf-8")
        raise self.exc


class _WorkerShutdownRunner:
    def run(self, _inp_path: Path):
        raise WorkerShutdownInterrupt


class _WorkerShutdownWithScratchRunner:
    def run(self, inp_path: Path):
        exc = WorkerShutdownInterrupt()
        attach_scratch_provenance_to_exception(
            exc,
            ScratchPublication(
                paths=(inp_path.with_suffix(".gbw"), inp_path.with_suffix(".out")),
                omitted_transient_files=(f"{inp_path.stem}.EIJ.tmp",),
                omitted_transient_bytes=128,
            ),
        )
        raise exc


class _PublishedSuccessRunner:
    def run(self, inp_path: Path):
        out_path = inp_path.with_suffix(".out")
        out_path.write_text("published output\n", encoding="utf-8")
        return SimpleNamespace(
            out_path=str(out_path),
            return_code=0,
            scratch_provenance={
                "used": True,
                "filesystem": "tmpfs",
                "publication_status": "committed",
                "published_files": [inp_path.with_suffix(".gbw").name, out_path.name],
                "omitted_transient_files": [],
                "omitted_transient_bytes": 0,
            },
        )


class _AlwaysScfFailRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        inp_path.with_suffix(".xyz").write_text(
            "2\ncheckpoint geometry\nH 0 0 0\nH 0 0 0.75\n",
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
        return SimpleNamespace(
            out_path=str(out_path),
            return_code=0,
            command=("/opt/orca/orca", inp_path.name),
            input_identity={
                "path": str(inp_path),
                "sha256": "a" * 64,
                "size_bytes": inp_path.stat().st_size,
            },
            executable_identity={
                "path": "/opt/orca/orca",
                "sha256": "b" * 64,
                "size_bytes": 1024,
            },
        )


class TestAttemptEngine(unittest.TestCase):
    def test_keyboard_interrupt_emits_single_run_interrupted_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)

            emitted_payloads = []

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=_InterruptRunner(),
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

    def test_in_place_failure_preserves_existing_output_path(self) -> None:
        cases = (
            (KeyboardInterrupt(), 130, "interrupted_by_user"),
            (RuntimeError("injected runner failure"), 1, "runner_exception"),
        )
        for exc, expected_rc, expected_reason in cases:
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as td:
                reaction_dir = Path(td)
                selected_inp = reaction_dir / "rxn.inp"
                selected_inp.write_text("! SP\n", encoding="utf-8")
                state = new_state(reaction_dir, selected_inp)

                rc = run_attempts(
                    reaction_dir,
                    selected_inp,
                    state,
                    resumed=False,
                    runner=_InPlaceFailureRunner(exc),
                    emit=lambda _payload: None,
                )
                saved = load_state(reaction_dir)

                assert saved is not None
                final_result = saved["final_result"]
                assert final_result is not None
                self.assertEqual(rc, expected_rc)
                self.assertEqual(final_result["reason"], expected_reason)
                self.assertEqual(saved["attempts"], [])
                self.assertEqual(saved.get("scratch_publications", []), [])
                self.assertEqual(
                    final_result["last_out_path"],
                    str(selected_inp.with_suffix(".out")),
                )

    def test_in_place_failure_does_not_report_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! SP\n", encoding="utf-8")
            outside_out = reaction_dir / "outside.out"
            outside_out.write_text("outside\n", encoding="utf-8")
            selected_inp.with_suffix(".out").symlink_to(outside_out)
            state = new_state(reaction_dir, selected_inp)

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=_InterruptRunner(),
                emit=lambda _payload: None,
            )
            saved = load_state(reaction_dir)

        assert saved is not None
        final_result = saved["final_result"]
        assert final_result is not None
        self.assertEqual(rc, 130)
        self.assertIsNone(final_result["last_out_path"])

    def test_worker_shutdown_propagates_without_failed_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)

            emitted_payloads = []

            with self.assertRaises(WorkerShutdownInterrupt):
                run_attempts(
                    reaction_dir,
                    selected_inp,
                    state,
                    resumed=False,
                    runner=_WorkerShutdownRunner(),
                    emit=lambda payload: emitted_payloads.append(payload),
                )

            saved = load_state(reaction_dir)

        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertIsNone(saved["final_result"])
        self.assertEqual(saved["status"], "running")
        self.assertEqual(emitted_payloads, [])

    def test_worker_shutdown_persists_committed_scratch_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! SP\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)

            with self.assertRaises(WorkerShutdownInterrupt):
                run_attempts(
                    reaction_dir,
                    selected_inp,
                    state,
                    resumed=False,
                    runner=_WorkerShutdownWithScratchRunner(),
                    emit=lambda _payload: None,
                )

            saved = load_state(reaction_dir)

        assert saved is not None
        self.assertEqual(saved["attempts"], [])
        self.assertIsNone(saved["final_result"])
        publication = saved["scratch_publications"][0]
        self.assertEqual(publication["attempt_index"], 1)
        self.assertEqual(publication["outcome"], "worker_shutdown")
        self.assertEqual(
            publication["publication"]["published_files"],
            ["rxn.gbw", "rxn.out"],
        )

    def test_analyzer_exception_persists_committed_scratch_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! SP\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)

            with patch(
                "orca_auto.orca.attempt.engine.analyze_output",
                side_effect=RuntimeError("injected analyzer failure"),
            ):
                rc = run_attempts(
                    reaction_dir,
                    selected_inp,
                    state,
                    resumed=False,
                    runner=_PublishedSuccessRunner(),
                    emit=lambda _payload: None,
                )

            saved = load_state(reaction_dir)

        assert saved is not None
        self.assertEqual(rc, 1)
        self.assertEqual(saved["attempts"], [])
        self.assertEqual(saved["scratch_publications"][0]["outcome"], "exception")
        assert saved["final_result"] is not None
        self.assertEqual(
            saved["final_result"]["last_out_path"], str(selected_inp.with_suffix(".out"))
        )

    def test_opt_failure_is_terminal_despite_restart_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)
            runner = _AlwaysScfFailRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                emit=lambda _payload: None,
            )

            retry_inp = reaction_dir / "rxn.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry_inp.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertNotIn("max_retries", saved)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "scf_not_converged")

    def test_scf_failure_is_terminal_without_restart_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)
            runner = _NoArtifactScfFailRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                emit=lambda _payload: None,
            )

            retry_inp = reaction_dir / "rxn.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry_inp.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertNotIn("max_retries", saved)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "scf_not_converged")

    def test_neb_ts_failure_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "neb.inp"
            selected_inp.write_text(
                "! NEB-TS B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
            state = new_state(reaction_dir, selected_inp)
            runner = _NoImaginaryModeRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=runner,
                emit=lambda _payload: None,
            )

            retry01 = reaction_dir / "neb.retry01.inp"
            saved = load_state(reaction_dir)

        self.assertEqual(rc, 1)
        self.assertEqual(runner.seen, [selected_inp])
        self.assertFalse(retry01.exists())
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertNotIn("max_retries", saved)
        final_result = saved.get("final_result")
        assert final_result is not None
        self.assertEqual(final_result.get("reason"), "ts_criteria_failed")

    def test_start_and_finish_callbacks_emit_immediate_terminal_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction_dir = Path(td)
            selected_inp = reaction_dir / "rxn.inp"
            selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            state = new_state(reaction_dir, selected_inp)
            started_notifications = []
            finished_notifications = []

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=False,
                runner=_CaptureSuccessRunner(),
                emit=lambda _payload: None,
                notify_started=lambda payload: started_notifications.append(payload),
                notify_finished=lambda payload: finished_notifications.append(payload),
            )

        self.assertEqual(rc, 0)
        self.assertEqual(len(started_notifications), 1)
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
            state = new_state(reaction_dir, selected_inp)
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
            state = new_state(reaction_dir, selected_inp)
            runner = _CaptureSuccessRunner()

            rc = run_attempts(
                reaction_dir,
                selected_inp,
                state,
                resumed=True,
                runner=runner,
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
        self.assertEqual(
            saved["attempts"][0]["command"],
            ["/opt/orca/orca", resume_inp.name],
        )
        self.assertEqual(saved["attempts"][0]["input_identity"]["path"], str(resume_inp))
        self.assertEqual(
            saved["attempts"][0]["executable_identity"]["sha256"],
            "b" * 64,
        )
        self.assertIn(
            "resume_checkpoint_restart_from_rxn.gbw", saved["attempts"][0]["patch_actions"]
        )
        self.assertIn("resume_geometry_restart_from_rxn.xyz", saved["attempts"][0]["patch_actions"])


class _NoImaginaryModeRunner:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def run(self, inp_path: Path):
        self.seen.append(inp_path)
        out_path = inp_path.with_suffix(".out")
        out_path.write_text(
            "VIBRATIONAL FREQUENCIES\n  1    120.00 cm**-1\n  2    240.00 cm**-1\n****ORCA TERMINATED NORMALLY****\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_path=str(out_path), return_code=0)
