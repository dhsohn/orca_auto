import io
import json
import logging
import os
import tempfile
import time
import unittest
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_auto import cli as unified_cli
from orca_auto import cli_handlers as cli_run_dir
from orca_auto.core.admission import reserve_slot
from orca_auto.orca.cli_logging import (
    configure_logging as _configure_logging,
)
from orca_auto.orca.cli_logging import (
    remove_managed_handlers as _remove_managed_handlers,
)
from orca_auto.orca.commands._helpers import CONFIG_ENV_VAR, _emit, default_config_path
from orca_auto.orca.commands.run_inp import (
    _cmd_run_inp_execute,
    _existing_completed_out,
    _retry_inp_path,
    _select_latest_inp,
)
from orca_auto.orca.orca_runner import RunResult, WorkerShutdownInterrupt
from orca_auto.orca.state import load_state, save_state, state_path
from orca_auto.orca.types import RunFinalResult, RunState

build_parser = unified_cli.build_parser
main = unified_cli.main


def _loaded_state(reaction_dir: Path) -> RunState:
    state = load_state(reaction_dir)
    assert state is not None
    return state


def _final_result(state: RunState) -> RunFinalResult:
    final_result = state["final_result"]
    assert final_result is not None
    return final_result


class TestCli(unittest.TestCase):
    def _write_config(
        self, root: Path, allowed_root: Path, *, telegram_enabled: bool = False
    ) -> Path:
        fake_orca = root / "fake_orca"
        fake_orca.touch()
        fake_orca.chmod(0o755)
        payload = {
            "orca": {
                "runtime": {
                    "allowed_root": str(allowed_root),
                    "default_max_retries": 2,
                },
                "paths": {"orca_executable": str(fake_orca)},
            },
        }
        if telegram_enabled:
            payload["telegram"] = {
                "bot_token": "123:ABC",
                "chat_id": "999",
            }
        config = root / "orca_auto.yaml"
        config.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return config

    def _run_internal_execute(
        self, config: Path, reaction_dir: Path, *, force: bool = False
    ) -> int:
        # Shared admission slots live in the hidden .admission directory
        # under the runs root (= allowed_root).
        token = reserve_slot(
            reaction_dir.parent / ".admission",
            1,
            work_dir=str(reaction_dir),
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        return _cmd_run_inp_execute(
            Namespace(
                config=str(config),
                reaction_dir=str(reaction_dir),
                force=force,
            ),
            reservation_token=token,
        )

    def test_rejects_outside_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (outside / "a.inp").write_text(
                "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
            config = self._write_config(root, allowed)

            rc = main(["run-dir", "--config", str(config), str(outside)])
        self.assertEqual(rc, 1)

    def test_select_latest_inp_prefers_base_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            base = reaction / "rxn.inp"
            retry = reaction / "rxn.retry01.inp"
            base.write_text("! Opt\n", encoding="utf-8")
            time.sleep(0.01)
            retry.write_text("! Opt\n", encoding="utf-8")
            selected = _select_latest_inp(reaction)
        self.assertEqual(selected.name, "rxn.inp")

    def test_select_latest_inp_warns_when_multiple_base_inputs_exist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            older = reaction / "a.inp"
            newer = reaction / "b.inp"
            older.write_text("! Opt\n", encoding="utf-8")
            time.sleep(0.01)
            newer.write_text("! Opt\n", encoding="utf-8")

            with self.assertLogs(
                "orca_auto.orca.commands.run_inp_execution",
                level="WARNING",
            ) as logs:
                selected = _select_latest_inp(reaction)

        self.assertEqual(selected.name, "b.inp")
        self.assertIn("Multiple ORCA .inp candidates", "\n".join(logs.output))

    def test_retry_inp_path_uses_canonical_base_stem(self) -> None:
        retry_base = Path("/tmp/rxn.retry03.inp")
        retry_next = _retry_inp_path(retry_base, 1)
        self.assertEqual(retry_next.name, "rxn.retry01.inp")

    def test_existing_completed_out_ignores_stale_output_older_than_selected_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reaction = Path(td)
            inp = reaction / "rxn.inp"
            out = reaction / "rxn.out"
            inp.write_text("! Opt\n", encoding="utf-8")
            out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
            os.utime(out, ns=(1_000_000_000, 1_000_000_000))
            os.utime(inp, ns=(2_000_000_000, 2_000_000_000))

            done = _existing_completed_out(inp)

        self.assertIsNone(done)

    def test_default_config_path_prefers_env_var(self) -> None:
        with patch.dict(os.environ, {CONFIG_ENV_VAR: "/tmp/custom_orca_auto.yaml"}, clear=False):
            self.assertEqual(default_config_path(), "/tmp/custom_orca_auto.yaml")

    def test_run_dir_accepts_queue_submission_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-dir",
                "/tmp/rxn",
                "--priority",
                "3",
                "--max-cores",
                "16",
                "--max-memory-gb",
                "64",
            ]
        )

        self.assertEqual(args.command, "run-dir")
        self.assertEqual(args.path, "/tmp/rxn")
        self.assertEqual(args.priority, 3)
        self.assertEqual(args.max_cores, 16)
        self.assertEqual(args.max_memory_gb, 64)

    def test_run_dir_rejects_foreground_flag(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as exc:
            parser.parse_args(["run-dir", "/tmp/rxn", "--foreground"])
        self.assertEqual(exc.exception.code, 2)

    @patch("orca_auto.cli_handlers.cmd_run_dir", return_value=8)
    def test_main_dispatches_run_dir_command(self, mock_cmd_run_dir: MagicMock) -> None:
        rc = main(["run-dir", "/tmp/rxn"])

        self.assertEqual(rc, 8)
        mock_cmd_run_dir.assert_called_once()

    @patch("orca_auto.cli_queue.cmd_queue_list", return_value=9)
    def test_main_dispatches_list_command(self, mock_cmd_list: MagicMock) -> None:
        rc = main(["queue", "list", "--engine", "orca"])

        self.assertEqual(rc, 9)
        mock_cmd_list.assert_called_once()

    def test_configure_logging_replaces_previous_orca_auto_handler(self) -> None:
        root_logger = logging.getLogger()
        original_level = root_logger.level
        original_handlers = list(root_logger.handlers)
        for handler in list(root_logger.handlers):
            if getattr(handler, "_orca_auto_managed_handler", False):
                root_logger.removeHandler(handler)
                handler.close()

        try:
            _configure_logging(Namespace(verbose=False, log_file=None))
            _configure_logging(Namespace(verbose=True, log_file=None))

            managed_handlers = [
                handler
                for handler in root_logger.handlers
                if getattr(handler, "_orca_auto_managed_handler", False)
            ]
            self.assertEqual(len(managed_handlers), 1)
            self.assertEqual(root_logger.level, logging.DEBUG)
        finally:
            for handler in list(root_logger.handlers):
                if getattr(handler, "_orca_auto_managed_handler", False):
                    root_logger.removeHandler(handler)
                    handler.close()
            root_logger.setLevel(original_level)
            current_handlers = list(root_logger.handlers)
            for handler in current_handlers:
                if handler not in original_handlers:
                    root_logger.removeHandler(handler)
            for handler in original_handlers:
                if handler not in root_logger.handlers:
                    root_logger.addHandler(handler)

    def test_queue_add_is_not_a_valid_subcommand(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as exc:
            parser.parse_args(["queue", "add"])
        self.assertEqual(exc.exception.code, 2)

    def test_cmd_run_inp_dispatches_to_orca_command_module(self) -> None:
        seen: list[Namespace] = []

        def _fake_run_inp(args: Namespace) -> int:
            seen.append(args)
            return 41

        with patch("orca_auto.orca.commands.run_inp.cmd_run_inp", side_effect=_fake_run_inp):
            args = Namespace(
                config="/tmp/orca_auto.yaml",
                verbose=True,
                log_file="/tmp/orca.log",
                path="/tmp/rxn",
                priority=3,
                force=True,
                max_cores=12,
                max_memory_gb=48,
            )
            rc = cli_run_dir.cmd_orca_run_dir(args)

        self.assertEqual(rc, 41)
        self.assertEqual(seen, [args])

    def test_other_public_wrappers_dispatch_to_orca_command_modules(self) -> None:
        seen: list[tuple[str, Namespace]] = []

        def _record(name: str, return_code: int) -> Callable[[Namespace], int]:
            def _inner(args: Namespace) -> int:
                seen.append((name, args))
                return return_code

            return _inner

        with patch("orca_auto.orca.commands.init.cmd_init", side_effect=_record("init", 42)):
            init_args = Namespace(
                config="/tmp/orca_auto.yaml",
                verbose=False,
                log_file=None,
                force=True,
            )
            init_rc = cli_run_dir.cmd_init(init_args)

        self.assertEqual(init_rc, 42)
        self.assertEqual(seen, [("init", init_args)])

    @patch("orca_auto.orca.cli_logging.remove_managed_handlers")
    @patch("orca_auto.orca.cli_logging.logging.handlers.RotatingFileHandler")
    @patch("orca_auto.orca.cli_logging.logging.getLogger")
    def test_configure_logging_uses_rotating_file_handler_when_log_file_is_set(
        self,
        mock_get_logger: MagicMock,
        mock_rotating_handler: MagicMock,
        mock_remove_handlers: MagicMock,
    ) -> None:
        root_logger = MagicMock()
        mock_get_logger.return_value = root_logger
        handler = MagicMock(spec=logging.Handler)
        mock_rotating_handler.return_value = handler

        _configure_logging(Namespace(verbose=False, log_file="/tmp/orca_auto.log"))

        mock_remove_handlers.assert_called_once_with(root_logger)
        mock_rotating_handler.assert_called_once_with(
            "/tmp/orca_auto.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        root_logger.setLevel.assert_called_once_with(logging.INFO)
        handler.setFormatter.assert_called_once()
        root_logger.addHandler.assert_called_once_with(handler)

    def test_remove_managed_handlers_ignores_close_errors(self) -> None:
        root_logger = logging.Logger("test_cli_remove_managed")
        root_logger.handlers = []

        unmanaged = logging.StreamHandler()
        managed = MagicMock(spec=logging.Handler)
        managed._orca_auto_managed_handler = True
        managed.close.side_effect = RuntimeError("boom")

        root_logger.addHandler(unmanaged)
        root_logger.addHandler(managed)

        _remove_managed_handlers(root_logger)

        self.assertIn(unmanaged, root_logger.handlers)
        self.assertNotIn(managed, root_logger.handlers)

    def test_run_dir_queues_existing_completed_out_for_worker_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn1"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            (reaction / "rxn.out").write_text(
                "****ORCA TERMINATED NORMALLY****\n", encoding="utf-8"
            )
            config = self._write_config(root, root / "orca_runs")

            rc = main(["run-dir", "--config", str(config), str(reaction)])

        self.assertEqual(rc, 0)
        self.assertFalse(state_path(reaction).exists())

    def test_run_dir_queues_existing_completed_retry_out_for_worker_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn1_retry_done"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            retry_inp = reaction / "rxn.retry01.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            retry_inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
            (reaction / "rxn.out").write_text("SCF NOT CONVERGED\n", encoding="utf-8")
            (reaction / "rxn.retry01.out").write_text(
                "****ORCA TERMINATED NORMALLY****\n", encoding="utf-8"
            )
            config = self._write_config(root, root / "orca_runs")

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run") as run_mock:
                rc = main(["run-dir", "--config", str(config), str(reaction)])
            self.assertFalse(run_mock.called)
        self.assertEqual(rc, 0)
        self.assertFalse(state_path(reaction).exists())

    def test_skip_existing_completed_out_still_respects_run_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn1_locked"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            (reaction / "rxn.out").write_text(
                "****ORCA TERMINATED NORMALLY****\n", encoding="utf-8"
            )
            (reaction / "run.lock").write_text(
                json.dumps({"pid": os.getpid(), "started_at": "2026-02-24T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
            config = self._write_config(root, root / "orca_runs")

            rc = main(["run-dir", "--config", str(config), str(reaction)])

        self.assertEqual(rc, 1)
        self.assertFalse(state_path(reaction).exists())

    def test_run_dir_preserves_existing_state_until_worker_reconciles_completed_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn1_resume_skip"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            out = reaction / "rxn.out"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")
            state = {
                "run_id": "run_resume_skip_existing_out",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 5,
                "status": "failed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(out),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "analyzer_reason": "run_incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    }
                ],
                "final_result": {
                    "status": "failed",
                    "analyzer_status": "incomplete",
                    "reason": "worker_shutdown",
                    "completed_at": "2026-01-01T00:00:02+00:00",
                    "last_out_path": str(out),
                },
            }
            save_state(reaction, state)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run") as run_mock:
                rc = main(["run-dir", "--config", str(config), str(reaction)])
            self.assertFalse(run_mock.called)
            saved = _loaded_state(reaction)

        self.assertEqual(rc, 0)
        self.assertEqual(saved["run_id"], "run_resume_skip_existing_out")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(_final_result(saved)["reason"], "worker_shutdown")

    def test_worker_shutdown_propagates_without_finalizing_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn5_worker_shutdown"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")

            def _fake_run(_self, inp_path: Path) -> RunResult:
                raise WorkerShutdownInterrupt

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                with self.assertRaises(WorkerShutdownInterrupt):
                    self._run_internal_execute(config, reaction)
            saved = _loaded_state(reaction)

        self.assertEqual(saved["status"], "running")
        self.assertIsNone(saved["final_result"])
        self.assertEqual(len(saved["attempts"]), 0)

    def test_standalone_optts_failure_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn2"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text(
                "! OptTS Freq IRC\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
            )
            config = self._write_config(root, root / "orca_runs")

            calls = {"n": 0}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                calls["n"] += 1
                out = inp_path.with_suffix(".out")
                out.write_text(
                    "ORCA finished by error termination in SCF gradient\n", encoding="utf-8"
                )
                return RunResult(out_path=str(out), return_code=55)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)

            state = _loaded_state(reaction)
            retry_exists = (reaction / "rxn.retry01.inp").exists()
        self.assertEqual(rc, 1)
        self.assertEqual(calls["n"], 1)
        self.assertFalse(retry_exists)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["max_retries"], 0)
        self.assertEqual(len(state["attempts"]), 1)

    @patch("orca_auto.orca.commands.run_inp.notify_retry_event", return_value=True)
    def test_standalone_optts_retry_flow_does_not_send_telegram_notification(
        self, mock_notify: MagicMock
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_notify"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text(
                "! OptTS B3LYP def2-SVP Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )
            config = self._write_config(root, root / "orca_runs", telegram_enabled=True)

            calls = {"n": 0}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                calls["n"] += 1
                out = inp_path.with_suffix(".out")
                out.write_text("SCF NOT CONVERGED AFTER 300 CYCLES\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=1)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)

        self.assertEqual(rc, 1)
        self.assertEqual(calls["n"], 1)
        mock_notify.assert_not_called()

    def test_disk_io_error_without_restart_artifacts_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_disk"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")
            calls = {"n": 0}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                calls["n"] += 1
                out = inp_path.with_suffix(".out")
                out.write_text("COULD NOT WRITE TO DISK\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=99)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            state = _loaded_state(reaction)
            retry01_exists = (reaction / "rxn.retry01.inp").exists()

        self.assertEqual(rc, 1)
        self.assertEqual(calls["n"], 1)
        self.assertFalse(retry01_exists)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(state["attempts"]), 1)
        final_result = _final_result(state)
        self.assertEqual(final_result["reason"], "retry_limit_reached")
        self.assertEqual(final_result["analyzer_status"], "error_disk_io")

    def test_positive_default_max_retries_uses_route_policy_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_disk_long"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            fake_orca = root / "fake_orca"
            fake_orca.touch()
            fake_orca.chmod(0o755)
            config = root / "orca_auto.yaml"
            config.write_text(
                json.dumps(
                    {
                        "orca": {
                            "runtime": {
                                "allowed_root": str(root / "orca_runs"),
                                "default_max_retries": 6,
                            },
                            "paths": {"orca_executable": str(fake_orca)},
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls = {"n": 0}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                calls["n"] += 1
                out = inp_path.with_suffix(".out")
                out.write_text("COULD NOT WRITE TO DISK\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=99)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            state = _loaded_state(reaction)

        self.assertEqual(rc, 1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(state["max_retries"], 0)
        self.assertEqual(len(state["attempts"]), 1)
        final_result = _final_result(state)
        self.assertEqual(final_result["reason"], "retry_limit_reached")
        self.assertEqual(final_result["analyzer_status"], "error_disk_io")

    def test_retry_limit_already_reached_finalizes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn4"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            fake_orca = root / "fake_orca"
            fake_orca.touch()
            fake_orca.chmod(0o755)
            config = root / "orca_auto.yaml"
            config.write_text(
                json.dumps(
                    {
                        "orca": {
                            "runtime": {
                                "allowed_root": str(root / "orca_runs"),
                                "default_max_retries": 0,
                            },
                            "paths": {"orca_executable": str(fake_orca)},
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = {
                "run_id": "run_test_resume",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 5,
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(reaction / "rxn.out"),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    },
                    {
                        "index": 2,
                        "inp_path": str(reaction / "rxn.retry01.inp"),
                        "out_path": str(reaction / "rxn.retry01.out"),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:02+00:00",
                        "ended_at": "2026-01-01T00:00:03+00:00",
                    },
                ],
                "final_result": None,
            }
            save_state(reaction, state)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run") as run_mock:
                rc = self._run_internal_execute(config, reaction)
            self.assertFalse(run_mock.called)
            saved = _loaded_state(reaction)

        self.assertEqual(rc, 1)
        self.assertEqual(saved["status"], "failed")
        final_result = _final_result(saved)
        self.assertEqual(final_result["reason"], "retry_limit_reached")
        self.assertEqual(final_result["last_out_path"], str(reaction / "rxn.retry01.out"))

    def test_resume_runs_prepared_retry_input_after_multiple_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_resume_prepared"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            (reaction / "rxn.retry02.inp").write_text(
                inp.read_text(encoding="utf-8"), encoding="utf-8"
            )
            config = self._write_config(root, root / "orca_runs")
            state = {
                "run_id": "run_resume_prepared",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 5,
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(reaction / "rxn.out"),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    },
                    {
                        "index": 2,
                        "inp_path": str(reaction / "rxn.retry01.inp"),
                        "out_path": str(reaction / "rxn.retry01.out"),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:02+00:00",
                        "ended_at": "2026-01-01T00:00:03+00:00",
                    },
                ],
                "final_result": None,
            }
            save_state(reaction, state)
            seen = {"inp_name": ""}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                seen["inp_name"] = inp_path.name
                out = inp_path.with_suffix(".out")
                out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=0)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            saved = _loaded_state(reaction)

        self.assertEqual(rc, 0)
        self.assertEqual(seen["inp_name"], "rxn.retry02.inp")
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["max_retries"], 5)
        self.assertEqual(len(saved["attempts"]), 3)

    def test_resume_recreates_missing_retry_input_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_resume"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")
            state = {
                "run_id": "run_resume_recover",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 5,
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(reaction / "rxn.out"),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "analyzer_reason": "run_incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    }
                ],
                "final_result": None,
            }
            save_state(reaction, state)
            seen = {"inp_name": ""}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                seen["inp_name"] = inp_path.name
                out = inp_path.with_suffix(".out")
                out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=0)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            saved = _loaded_state(reaction)
            retry_exists = (reaction / "rxn.retry01.inp").exists()

        self.assertEqual(rc, 0)
        self.assertEqual(seen["inp_name"], "rxn.retry01.inp")
        self.assertTrue(retry_exists)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(saved["attempts"]), 2)
        actions = saved["attempts"][0].get("patch_actions", [])
        self.assertTrue(
            any("resume_recreated_missing_input:rxn.retry01.inp" in action for action in actions)
        )

    def test_resume_interrupted_failure_keeps_run_id_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_resume_interrupt"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")
            state = {
                "run_id": "run_resume_interrupted",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 5,
                "status": "failed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(reaction / "rxn.out"),
                        "return_code": 1,
                        "analyzer_status": "incomplete",
                        "analyzer_reason": "run_incomplete",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    }
                ],
                "final_result": {
                    "status": "failed",
                    "analyzer_status": "incomplete",
                    "reason": "interrupted_by_user",
                    "completed_at": "2026-01-01T00:00:02+00:00",
                    "last_out_path": str(reaction / "rxn.out"),
                },
            }
            save_state(reaction, state)
            seen = {"inp_name": ""}

            def _fake_run(_self, inp_path: Path) -> RunResult:
                seen["inp_name"] = inp_path.name
                out = inp_path.with_suffix(".out")
                out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
                return RunResult(out_path=str(out), return_code=0)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            saved = _loaded_state(reaction)
            retry_exists = (reaction / "rxn.retry01.inp").exists()

        self.assertEqual(rc, 0)
        self.assertEqual(saved["run_id"], "run_resume_interrupted")
        self.assertEqual(seen["inp_name"], "rxn.retry01.inp")
        self.assertTrue(retry_exists)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(saved["attempts"]), 2)
        self.assertTrue(_final_result(saved)["resumed"])

    def test_resume_completed_attempt_finalizes_without_extra_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn_resume_done"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            out = reaction / "rxn.out"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            out.write_text("SCF NOT CONVERGED\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")
            state = {
                "run_id": "run_resume_completed",
                "reaction_dir": str(reaction),
                "selected_inp": str(inp),
                "max_retries": 5,
                "status": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "attempts": [
                    {
                        "index": 1,
                        "inp_path": str(inp),
                        "out_path": str(out),
                        "return_code": 0,
                        "analyzer_status": "completed",
                        "analyzer_reason": "normal_termination",
                        "markers": {},
                        "patch_actions": [],
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "ended_at": "2026-01-01T00:00:01+00:00",
                    }
                ],
                "final_result": None,
            }
            save_state(reaction, state)

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run") as run_mock:
                rc = self._run_internal_execute(config, reaction)
            self.assertFalse(run_mock.called)
            saved = _loaded_state(reaction)

        self.assertEqual(rc, 0)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(saved["attempts"]), 1)
        final_result = _final_result(saved)
        self.assertEqual(final_result["reason"], "normal_termination")
        self.assertTrue(final_result["resumed"])

    def test_keyboard_interrupt_stops_run_and_finalizes_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn5"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")

            def _fake_run(_self, inp_path: Path) -> RunResult:
                raise KeyboardInterrupt

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            saved = _loaded_state(reaction)

        self.assertEqual(rc, 130)
        self.assertEqual(saved["status"], "failed")
        final_result = _final_result(saved)
        self.assertEqual(final_result["reason"], "interrupted_by_user")
        self.assertEqual(final_result["analyzer_status"], "incomplete")
        self.assertEqual(len(saved["attempts"]), 0)

    def test_runner_exception_finalizes_state_with_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reaction = root / "orca_runs" / "rxn6"
            reaction.mkdir(parents=True)
            inp = reaction / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
            config = self._write_config(root, root / "orca_runs")

            def _fake_run(_self, inp_path: Path) -> RunResult:
                raise RuntimeError("runner exploded")

            with patch("orca_auto.orca.commands.run_inp.OrcaRunner.run", new=_fake_run):
                rc = self._run_internal_execute(config, reaction)
            saved = _loaded_state(reaction)

        self.assertEqual(rc, 1)
        self.assertEqual(saved["status"], "failed")
        final_result = _final_result(saved)
        self.assertEqual(final_result["reason"], "runner_exception")
        self.assertEqual(final_result["analyzer_status"], "incomplete")
        self.assertEqual(final_result["runner_error"], "runner exploded")
        self.assertEqual(len(saved["attempts"]), 0)

    def test_emit_plain_text_filters_known_keys(self) -> None:
        payload = {
            "status": "completed",
            "reaction_dir": "/tmp/rxn",
            "selected_inp": "/tmp/rxn/rxn.inp",
            "attempt_count": 1,
            "reason": "normal_termination",
            "run_state": "/tmp/rxn/job_state.json",
            "extra_unknown_key": "ignored",
        }
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            _emit(payload)
        output = captured.getvalue()
        self.assertIn("status: completed", output)
        self.assertIn("attempt_count: 1", output)
        self.assertNotIn("extra_unknown_key", output)

    def test_error_goes_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (outside / "a.inp").write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
            config = self._write_config(root, allowed)

            captured_stderr = io.StringIO()
            captured_stdout = io.StringIO()
            with patch("sys.stderr", captured_stderr), patch("sys.stdout", captured_stdout):
                rc = main(["run-dir", "--config", str(config), str(outside)])
        self.assertEqual(rc, 1)
        # Error should go to stderr (via logger.error), not stdout
        self.assertNotIn("allowed root", captured_stdout.getvalue())
