import errno
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.core.engine_scratch import scratch_provenance_from_exception
from orca_auto.core.queue.cancellable import ProcessCleanupError
from orca_auto.orca.orca_process import (
    ORCA_PROCESS_RECORD_FILE_NAME,
    OrcaProcessRecordCorruptError,
    OrcaProcessRecoveryError,
)
from orca_auto.orca.orca_runner import (
    OrcaRunner,
    ShutdownSignalGuard,
    WorkerShutdownInterrupt,
)
from orca_auto.orca.scratch import OrcaScratchPolicy


def _installed_signal_handler(
    mock_signal: MagicMock,
    signum: int = signal.SIGTERM,
) -> Callable[[int, object], None]:
    for signal_call in mock_signal.call_args_list:
        handler = signal_call.args[1]
        if signal_call.args[0] == signum and callable(handler):
            return handler
    raise AssertionError(f"no installed handler found for signal {signum}")


class OrcaRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        ticks_patcher = patch(
            "orca_auto.orca.orca_process.process_lock.process_start_ticks",
            return_value=12345,
        )
        ticks_patcher.start()
        self.addCleanup(ticks_patcher.stop)
        original_open = OrcaRunner._open_pinned_executable

        def open_test_executable(runner: OrcaRunner):
            if runner.orca_executable == "/opt/orca/orca":
                descriptor = os.open("/bin/true", os.O_RDONLY)
                details = os.fstat(descriptor)
                return descriptor, {
                    "path": runner.orca_executable,
                    "sha256": "test-double",
                    "size_bytes": int(details.st_size),
                }
            return original_open(runner)

        executable_patcher = patch.object(
            OrcaRunner,
            "_open_pinned_executable",
            open_test_executable,
        )
        executable_patcher.start()
        self.addCleanup(executable_patcher.stop)


class TestOrcaRunnerCommandConstruction(OrcaRunnerTestCase):
    def test_open_pinned_executable_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            executable = Path(td) / "fake-orca"
            os.mkfifo(executable)

            runner = OrcaRunner(str(executable))
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                runner._open_pinned_executable()

    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_command_uses_linux_binary(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            result = runner.run(inp)

        args, kwargs = mock_popen.call_args
        command = args[0]
        self.assertEqual(command[0], "/proc/self/exe")
        self.assertTrue(command[1].startswith("/proc/self/fd/"))
        launch_gate_fd = int(command[1].removeprefix("/proc/self/fd/"))
        self.assertEqual(int(command[2]), launch_gate_fd)
        self.assertEqual(command[3], "/opt/orca/orca")
        executable_fd = int(command[4])
        self.assertGreaterEqual(executable_fd, 3)
        self.assertEqual(command[5], "test.inp")
        self.assertIn(launch_gate_fd, kwargs["pass_fds"])
        self.assertIn(executable_fd, kwargs["pass_fds"])
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(result.command, ("/opt/orca/orca", "test.inp"))
        self.assertEqual(result.input_identity["path"], str(inp))
        self.assertEqual(result.input_identity["size_bytes"], len(b"! Opt\n"))

    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_launch_pins_thread_env_to_one(self, mock_popen: MagicMock) -> None:
        # ORCA parallelizes across %pal MPI ranks, so the launch env pins the
        # OpenMP/BLAS thread count to 1 (each rank single-threaded), matching the
        # discipline the other engines apply and preventing N^2 oversubscription.
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            runner.run(inp)

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        for var in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            self.assertEqual(env[var], "1")
        # The rest of the inherited environment is preserved, not replaced.
        self.assertIn("PATH", env)

    def test_ram_scratch_publishes_results_and_keeps_process_record_durable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_shm = root / "shm"
            fake_shm.mkdir()
            durable = root / "durable"
            durable.mkdir()
            executable = root / "fake_orca"
            executable.write_text(
                "#!/bin/sh\n"
                "stem=${1%.inp}\n"
                'printf checkpoint > "$stem.gbw"\n'
                'printf scratch > "$stem.EIJ.tmp"\n'
                "printf 'ORCA TERMINATED NORMALLY\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            inp = durable / "test.inp"
            inp.write_text("! SP\n", encoding="utf-8")

            runner = OrcaRunner(str(executable))
            with (
                patch.object(scratch_mod, "_SCRATCH_ROOT_PARENT", fake_shm),
                patch.object(
                    scratch_mod,
                    "_linux_available_memory_bytes",
                    return_value=2**63,
                ),
            ):
                runner.set_scratch_policy(
                    OrcaScratchPolicy(
                        root=fake_shm / "orca_auto",
                        min_free_bytes=1,
                        max_task_memory_bytes=1,
                    )
                )
                result = runner.run(inp)

            self.assertEqual(result.out_path, str(durable / "test.out"))
            self.assertEqual(result.input_identity["path"], str(inp))
            self.assertTrue((durable / "test.gbw").is_file())
            self.assertFalse((durable / "test.EIJ.tmp").exists())
            self.assertFalse((durable / ORCA_PROCESS_RECORD_FILE_NAME).exists())
            self.assertTrue(result.scratch_provenance["used"])
            self.assertEqual(
                result.scratch_provenance["omitted_transient_files"],
                ["test.EIJ.tmp"],
            )

    def test_ram_scratch_normalizes_only_the_private_input_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_shm = root / "shm"
            fake_shm.mkdir()
            durable = root / "durable"
            durable.mkdir()
            executable = root / "fake_orca"
            executable.write_text(
                "#!/bin/sh\n"
                'test "$(tail -c 1 "$1" | wc -l)" -eq 1 || exit 9\n'
                "printf 'ORCA TERMINATED NORMALLY\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            inp = durable / "test.inp"
            inp.write_bytes(b"! SP")

            runner = OrcaRunner(str(executable))
            with (
                patch.object(scratch_mod, "_SCRATCH_ROOT_PARENT", fake_shm),
                patch.object(
                    scratch_mod,
                    "_linux_available_memory_bytes",
                    return_value=2**63,
                ),
            ):
                runner.set_scratch_policy(
                    OrcaScratchPolicy(
                        root=fake_shm / "orca_auto",
                        min_free_bytes=1,
                        max_task_memory_bytes=1,
                    )
                )
                result = runner.run(inp)

            self.assertEqual(result.return_code, 0)
            self.assertEqual(inp.read_bytes(), b"! SP")
            self.assertIn("test.out", result.scratch_provenance["published_files"])

    def test_ram_scratch_publishes_checkpoint_before_shutdown_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_shm = root / "shm"
            fake_shm.mkdir()
            durable = root / "durable"
            durable.mkdir()
            executable = root / "slow_orca"
            executable.write_text(
                "#!/bin/sh\n"
                "stem=${1%.inp}\n"
                'printf checkpoint > "$stem.gbw"\n'
                'printf scratch > "$stem.EIJ.tmp"\n'
                "sleep 30\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            inp = durable / "test.inp"
            inp.write_text("! SP\n", encoding="utf-8")

            shutdown_checks = 0

            def shutdown_requested() -> bool:
                nonlocal shutdown_checks
                shutdown_checks += 1
                if shutdown_checks == 1:
                    return False
                time.sleep(0.1)
                return True

            runner = OrcaRunner(str(executable))
            runner.set_shutdown_requested(shutdown_requested)
            with (
                patch.object(scratch_mod, "_SCRATCH_ROOT_PARENT", fake_shm),
                patch.object(
                    scratch_mod,
                    "_linux_available_memory_bytes",
                    return_value=2**63,
                ),
            ):
                runner.set_scratch_policy(
                    OrcaScratchPolicy(
                        root=fake_shm / "orca_auto",
                        min_free_bytes=1,
                        max_task_memory_bytes=1,
                    )
                )
                with self.assertRaises(WorkerShutdownInterrupt) as caught:
                    runner.run(inp)

            self.assertEqual((durable / "test.gbw").read_bytes(), b"checkpoint")
            self.assertIn("interrupted by worker shutdown", (durable / "test.out").read_text())
            self.assertFalse((durable / "test.EIJ.tmp").exists())
            self.assertEqual(
                scratch_provenance_from_exception(caught.exception)["published_files"],
                ["test.gbw", "test.out"],
            )
            self.assertFalse(
                any(path.name.startswith("attempt-") for path in (fake_shm / "orca_auto").iterdir())
            )

    def test_ram_scratch_executes_through_pinned_workspace_after_root_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_shm = root / "shm"
            fake_shm.mkdir()
            durable = root / "durable"
            durable.mkdir()
            executable = root / "fake_orca"
            executable.write_text(
                "#!/bin/sh\nprintf 'ORCA TERMINATED NORMALLY\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            inp = durable / "test.inp"
            inp.write_text("! SP\n", encoding="utf-8")
            moved_root = fake_shm / "orca_auto-moved"

            runner = OrcaRunner(str(executable))
            original_create = scratch_mod.EngineScratchWorkspace.create

            def replace_root_after_create(*args, **kwargs):
                workspace = original_create(*args, **kwargs)
                workspace.policy.root.rename(moved_root)
                workspace.policy.root.mkdir()
                replacement = workspace.policy.root / workspace.path.name
                replacement.mkdir()
                (replacement / inp.name).write_bytes(inp.read_bytes())
                return workspace

            with (
                patch.object(scratch_mod, "_SCRATCH_ROOT_PARENT", fake_shm),
                patch.object(
                    scratch_mod,
                    "_linux_available_memory_bytes",
                    return_value=2**63,
                ),
                patch.object(
                    scratch_mod.EngineScratchWorkspace,
                    "create",
                    side_effect=replace_root_after_create,
                ),
            ):
                runner.set_scratch_policy(
                    OrcaScratchPolicy(
                        root=fake_shm / "orca_auto",
                        min_free_bytes=1,
                        max_task_memory_bytes=1,
                    )
                )
                with self.assertRaisesRegex(
                    scratch_mod.EngineScratchError,
                    "workspace pathname identity changed",
                ):
                    runner.run(inp)

            moved_workspace = next(
                path for path in moved_root.iterdir() if path.name.startswith("attempt-")
            )
            self.assertIn(
                "ORCA TERMINATED NORMALLY",
                (moved_workspace / "test.out").read_text(encoding="utf-8"),
            )
            replacement_workspace = fake_shm / "orca_auto" / moved_workspace.name
            self.assertFalse((replacement_workspace / "test.out").exists())
            self.assertFalse((durable / "test.out").exists())


class TestOrcaRunnerTermination(OrcaRunnerTestCase):
    def test_terminate_noop_when_process_already_exited(self) -> None:
        runner = OrcaRunner("/opt/orca/orca")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        with (
            patch("orca_auto.orca.orca_runner.os.killpg") as killpg,
            patch(
                "orca_auto.orca.orca_runner.process_group_is_alive",
                return_value=False,
            ),
        ):
            assert runner._terminate_subprocess_tree(mock_proc)
        killpg.assert_not_called()

    @patch("orca_auto.orca.orca_runner.os.killpg")
    def test_terminate_sends_sigterm_and_sigkill_on_timeout(self, mock_killpg: MagicMock) -> None:
        runner = OrcaRunner("/opt/orca/orca")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="orca", timeout=3),
            subprocess.TimeoutExpired(cmd="orca", timeout=5),
        ]

        result = runner._terminate_subprocess_tree(mock_proc)
        self.assertFalse(result)  # never exited -> termination not confirmed
        self.assertEqual(
            mock_killpg.mock_calls,
            [
                call(99999, signal.SIGTERM),
                call(99999, signal.SIGKILL),
            ],
        )

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_run_sigterm_terminates_orca_tree(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999

        def _wait(*_args: object, **_kwargs: object) -> int:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            raise subprocess.TimeoutExpired(cmd="orca", timeout=0.2)

        mock_proc.wait.side_effect = _wait
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with patch.object(runner, "_terminate_subprocess_tree") as terminate:
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        terminate.assert_called_once_with(mock_proc)
        self.assertEqual(mock_signal.call_args_list[0].args[0], signal.SIGTERM)
        self.assertIn(call(signal.SIGTERM, signal.SIG_DFL), mock_signal.call_args_list)
        self.assertIn(call(signal.SIGINT, signal.SIG_DFL), mock_signal.call_args_list)


class TestOrcaRunnerProcessRecordLifecycle(OrcaRunnerTestCase):
    @patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_normal_exit_clears_process_record(
        self, mock_popen: MagicMock, _group_alive: MagicMock
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            runner.run(inp)
            self.assertFalse((Path(td) / ORCA_PROCESS_RECORD_FILE_NAME).exists())

    @patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=True)
    @patch("orca_auto.orca.orca_runner.os.kill", return_value=None)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_normal_exit_does_not_signal_reused_pid_group(
        self,
        mock_popen: MagicMock,
        _pid_exists: MagicMock,
        group_alive: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! SP\n", encoding="utf-8")
            runner.run(inp)
            self.assertFalse((Path(td) / ORCA_PROCESS_RECORD_FILE_NAME).exists())

        group_alive.assert_not_called()

    @patch("orca_auto.orca.orca_runner.process_group_is_alive")
    @patch(
        "orca_auto.orca.orca_runner.os.kill",
        side_effect=OSError(errno.EIO, "probe failed"),
    )
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_normal_exit_retains_record_when_pid_reuse_probe_is_unknown(
        self,
        mock_popen: MagicMock,
        _pid_probe: MagicMock,
        group_alive: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! SP\n", encoding="utf-8")
            with self.assertRaises(OrcaProcessRecoveryError):
                runner.run(inp)
            self.assertTrue((Path(td) / ORCA_PROCESS_RECORD_FILE_NAME).exists())

        group_alive.assert_not_called()

    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_missing_start_ticks_aborts_launch_after_confirmed_cleanup(
        self,
        mock_popen: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc
        runner = OrcaRunner("/opt/orca/orca")

        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            with patch(
                "orca_auto.orca.orca_process.process_lock.process_start_ticks",
                return_value=None,
            ):
                with patch.object(
                    runner, "_terminate_subprocess_tree", return_value=True
                ) as terminate:
                    with self.assertRaises(OrcaProcessRecordCorruptError):
                        runner.run(inp)

            terminate.assert_called_once_with(mock_proc)
            self.assertFalse((Path(td) / ORCA_PROCESS_RECORD_FILE_NAME).exists())

    @patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_launch_gate_release_failure_clears_durable_process_record(
        self,
        mock_popen: MagicMock,
        _group_alive: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_proc.stdin.write.side_effect = OSError("release failed")
        mock_popen.return_value = mock_proc
        runner = OrcaRunner("/opt/orca/orca")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "test.inp"
            inp.write_text("! SP\n", encoding="utf-8")
            with patch.object(runner, "_terminate_subprocess_tree", return_value=True):
                with self.assertRaisesRegex(OSError, "release failed"):
                    runner.run(inp)

            self.assertFalse((root / ORCA_PROCESS_RECORD_FILE_NAME).exists())

    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_record_init_cleanup_failure_keeps_marker_until_process_exit(
        self,
        mock_popen: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        runner = OrcaRunner("/opt/orca/orca")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "test.inp"
            marker = root / ORCA_PROCESS_RECORD_FILE_NAME
            inp.write_text("! Opt\n", encoding="utf-8")
            poll_results = iter([None, 0])

            def poll() -> int | None:
                self.assertTrue(marker.exists())
                return next(poll_results)

            mock_proc.poll.side_effect = poll
            with patch(
                "orca_auto.orca.orca_process.process_lock.process_start_ticks",
                return_value=None,
            ):
                with patch.object(runner, "_terminate_subprocess_tree", return_value=False):
                    with self.assertRaises(ProcessCleanupError):
                        runner.run(inp)

            self.assertFalse(marker.exists())

    @patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=True)
    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_interrupt_keeps_record_while_group_survives(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
        _group_alive: MagicMock,
    ) -> None:
        # Leader reaped but a PAL/child process in the group is still running:
        # the record must survive so the next run's crash recovery reaps it.
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999

        def _wait(*_args: object, **_kwargs: object) -> int:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            return 0

        mock_proc.wait.side_effect = _wait
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            with patch.object(runner, "_terminate_subprocess_tree", return_value=True):
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)
            self.assertTrue((Path(td) / ORCA_PROCESS_RECORD_FILE_NAME).exists())

    @patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False)
    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_interrupt_clears_record_when_group_gone(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
        _group_alive: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999

        def _wait(*_args: object, **_kwargs: object) -> int:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            return 0

        mock_proc.wait.side_effect = _wait
        mock_popen.return_value = mock_proc

        runner = OrcaRunner("/opt/orca/orca")
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "test.inp"
            inp.write_text("! Opt\n", encoding="utf-8")
            with patch.object(runner, "_terminate_subprocess_tree", return_value=True):
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)
            self.assertFalse((Path(td) / ORCA_PROCESS_RECORD_FILE_NAME).exists())


class TestOrcaRunnerShutdownSignalGuard(OrcaRunnerTestCase):
    """Shutdown signals are state-only until the runner reaches a safe boundary."""

    def test_guard_records_only_the_first_signal_without_raising(self) -> None:
        protected_work_completed: list[bool] = []
        with (
            patch("orca_auto.orca.orca_runner.signal.signal"),
            patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL),
        ):
            with ShutdownSignalGuard() as guard:
                self.assertTrue(guard.installed)
                guard._handle_signal(signal.SIGTERM, None)
                guard._handle_signal(signal.SIGINT, None)
                protected_work_completed.append(True)
                self.assertTrue(guard.signalled)
                self.assertEqual(guard.received_signal, signal.SIGTERM)

        self.assertEqual(protected_work_completed, [True])

    def test_guard_does_not_mask_cleanup_failure_after_signal(self) -> None:
        with (
            patch("orca_auto.orca.orca_runner.signal.signal"),
            patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL),
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                with ShutdownSignalGuard() as guard:
                    guard._handle_signal(signal.SIGTERM, None)
                    raise RuntimeError("cleanup failed")

    def test_guard_survives_non_main_thread_install_failure(self) -> None:
        with (
            patch(
                "orca_auto.orca.orca_runner.signal.signal",
                side_effect=ValueError("not main thread"),
            ),
            patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL),
        ):
            with ShutdownSignalGuard() as guard:
                self.assertFalse(guard.installed)
                guard._handle_signal(signal.SIGTERM, None)

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_sigterm_during_cleanup_does_not_abort_process_tree_termination(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        """Reproduces TS8_wf/03_ts_guess: cancel SIGTERM lands while cleanup runs.

        Before the guard, the second signal escaped _retain_until_subprocess_tree_exits
        mid-way, leaving the reaped ORCA leader with a live process group -- reported as
        runner_exception rather than a cancellation.
        """
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999

        def _installed_handler() -> Callable[[int, object], None]:
            return _installed_signal_handler(mock_signal)

        def _wait(*_args: object, **_kwargs: object) -> int:
            _installed_handler()(signal.SIGTERM, None)  # first SIGTERM: marks the wait
            raise subprocess.TimeoutExpired(cmd="orca", timeout=0.2)

        mock_proc.wait.side_effect = _wait
        mock_popen.return_value = mock_proc

        cleanup_completed: list[bool] = []

        def _cleanup(proc: object) -> None:
            # a supervisor SIGTERM lands in the middle of terminating the tree
            _installed_handler()(signal.SIGTERM, None)
            cleanup_completed.append(True)

        runner = OrcaRunner("/opt/orca/orca")
        with patch.object(runner, "_retain_until_subprocess_tree_exits", side_effect=_cleanup):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        self.assertEqual(cleanup_completed, [True])
        self.assertIn(call(signal.SIGTERM, signal.SIG_DFL), mock_signal.call_args_list)
        self.assertIn(call(signal.SIGINT, signal.SIG_DFL), mock_signal.call_args_list)

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_sigterm_during_post_exit_retention_is_raised_after_bookkeeping(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        """A first SIGTERM during lingering-child cleanup must remain a cancellation."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        events: list[str] = []

        def _registrar(running: object | None) -> None:
            events.append("admission_registered" if running is not None else "admission_cleared")

        def _retain(_proc: object) -> None:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            events.append("process_tree_reaped")

        def _clear_record(*_args: object) -> None:
            events.append("process_record_cleared")

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_running_job_registrar(_registrar)
        with (
            patch("orca_auto.orca.orca_runner._reaped_pid_was_reused", return_value=False),
            patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=True),
            patch.object(runner, "_retain_until_subprocess_tree_exits", side_effect=_retain),
            patch.object(runner, "_clear_process_record_if_group_gone", side_effect=_clear_record),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        self.assertEqual(
            events,
            [
                "admission_registered",
                "process_tree_reaped",
                "process_record_cleared",
                "admission_cleared",
            ],
        )

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_polled_shutdown_keeps_signals_state_only_during_cleanup(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        cleanup_completed: list[bool] = []

        def _cleanup(_proc: object) -> None:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            cleanup_completed.append(True)

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_shutdown_requested(MagicMock(side_effect=[False, True]))
        with patch.object(runner, "_retain_until_subprocess_tree_exits", side_effect=_cleanup):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        self.assertEqual(cleanup_completed, [True])

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_repeated_sigint_during_cleanup_preserves_keyboard_interrupt(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999

        def _wait(*_args: object, **_kwargs: object) -> int:
            _installed_signal_handler(mock_signal, signal.SIGINT)(signal.SIGINT, None)
            raise subprocess.TimeoutExpired(cmd="orca", timeout=0.2)

        mock_proc.wait.side_effect = _wait
        mock_popen.return_value = mock_proc
        cleanup_completed: list[bool] = []

        def _cleanup(_proc: object) -> None:
            _installed_signal_handler(mock_signal, signal.SIGINT)(signal.SIGINT, None)
            cleanup_completed.append(True)

        runner = OrcaRunner("/opt/orca/orca")
        with patch.object(runner, "_retain_until_subprocess_tree_exits", side_effect=_cleanup):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(KeyboardInterrupt) as caught:
                    runner.run(inp)

        self.assertIs(type(caught.exception), KeyboardInterrupt)
        self.assertEqual(cleanup_completed, [True])

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_signal_delivered_during_handler_install_prevents_process_start(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        delivered = False

        def _install(signum: int, handler: object) -> None:
            nonlocal delivered
            if signum == signal.SIGTERM and callable(handler) and not delivered:
                delivered = True
                handler(signal.SIGTERM, None)

        mock_signal.side_effect = _install
        events: list[str] = []

        def _registrar(running: object | None) -> None:
            events.append("admission_registered" if running is not None else "admission_cleared")

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_running_job_registrar(_registrar)
        with (
            patch("orca_auto.orca.orca_runner._reaped_pid_was_reused", return_value=False),
            patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=True),
            patch.object(
                runner,
                "_retain_until_subprocess_tree_exits",
                side_effect=lambda _proc: events.append("process_tree_reaped"),
            ),
            patch.object(
                runner,
                "_clear_process_record_if_group_gone",
                side_effect=lambda *_args: events.append("process_record_cleared"),
            ),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        mock_popen.assert_not_called()
        self.assertEqual(events, ["admission_cleared"])

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_sigterm_during_bookkeeping_is_raised_after_admission_release(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        events: list[str] = []

        def _registrar(running: object | None) -> None:
            events.append("admission_registered" if running is not None else "admission_cleared")

        def _clear_record(*_args: object) -> None:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            events.append("process_record_cleared")

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_running_job_registrar(_registrar)
        with (
            patch("orca_auto.orca.orca_runner._reaped_pid_was_reused", return_value=False),
            patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=False),
            patch.object(runner, "_clear_process_record_if_group_gone", side_effect=_clear_record),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        self.assertEqual(
            events,
            ["admission_registered", "process_record_cleared", "admission_cleared"],
        )

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_interrupt_notice_failure_cannot_preempt_process_cleanup(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        _mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        events: list[str] = []

        def _failing_notice(_handle: object, message: str) -> None:
            failing_handle = MagicMock()
            failing_handle.write.side_effect = OSError(errno.ENOSPC, "disk full")
            OrcaRunner._write_interrupt_notice(failing_handle, message)
            events.append("notice_attempted")

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_shutdown_requested(MagicMock(side_effect=[False, True]))
        with (
            patch.object(
                runner,
                "_retain_until_subprocess_tree_exits",
                side_effect=lambda _proc: events.append("process_tree_reaped"),
            ),
            patch.object(runner, "_write_interrupt_notice", side_effect=_failing_notice),
            patch.object(
                runner,
                "_clear_process_record_if_group_gone",
                side_effect=lambda *_args: events.append("process_record_cleared"),
            ),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt):
                    runner.run(inp)

        self.assertEqual(
            events,
            ["process_tree_reaped", "notice_attempted", "process_record_cleared"],
        )

    def test_guard_blocks_cross_signal_until_both_handlers_are_installed(self) -> None:
        original_signal = signal.signal
        previous_handlers = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }
        original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        delivered = False

        def _install_with_pending_sigint(signum: int, handler: Any) -> Any:
            nonlocal delivered
            previous = original_signal(signum, handler)
            if signum == signal.SIGTERM and callable(handler) and not delivered:
                delivered = True
                signal.pthread_kill(threading.get_ident(), signal.SIGINT)
            return previous

        try:
            with patch(
                "orca_auto.orca.orca_runner.signal.signal",
                side_effect=_install_with_pending_sigint,
            ):
                with ShutdownSignalGuard() as guard:
                    self.assertEqual(guard.received_signal, signal.SIGINT)
        finally:
            signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGTERM, signal.SIGINT},
            )
            try:
                for signum, handler in previous_handlers.items():
                    original_signal(signum, handler)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_callback_failure_terminates_live_process_before_bookkeeping(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        _mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        events: list[str] = []

        def _registrar(running: object | None) -> None:
            events.append("admission_registered" if running is not None else "admission_cleared")

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_running_job_registrar(_registrar)
        runner.set_shutdown_requested(MagicMock(side_effect=[False, RuntimeError("poll failed")]))
        with (
            patch("orca_auto.orca.orca_runner._reaped_pid_was_reused", return_value=False),
            patch("orca_auto.orca.orca_runner.process_group_is_alive", return_value=True),
            patch.object(
                runner,
                "_retain_until_subprocess_tree_exits",
                side_effect=lambda _proc: events.append("process_tree_reaped"),
            ),
            patch.object(
                runner,
                "_clear_process_record_if_group_gone",
                side_effect=lambda *_args: events.append("process_record_cleared"),
            ),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "poll failed"):
                    runner.run(inp)

        self.assertEqual(
            events,
            [
                "admission_registered",
                "process_tree_reaped",
                "process_record_cleared",
                "admission_cleared",
            ],
        )

    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_signal_handlers_remain_active_through_cleanup_and_bookkeeping(
        self,
        mock_popen: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        events: list[str] = []

        def _previous_sigint(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        active_handlers: dict[int, Any] = {
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGINT: _previous_sigint,
        }

        def _getsignal(signum: int) -> Any:
            return active_handlers[signum]

        def _signal(signum: int, handler: Any) -> Any:
            previous = active_handlers[signum]
            active_handlers[signum] = handler
            return previous

        def _dispatch_sigint() -> None:
            handler = active_handlers[signal.SIGINT]
            if not callable(handler):
                raise AssertionError("SIGINT state handler was restored before cleanup")
            handler(signal.SIGINT, None)

        def _registrar(running: object | None) -> None:
            events.append("admission_registered" if running is not None else "admission_cleared")

        def _cleanup(_proc: object) -> None:
            _dispatch_sigint()
            events.append("process_tree_reaped")

        def _clear_record(*_args: object) -> None:
            _dispatch_sigint()
            events.append("process_record_cleared")

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_running_job_registrar(_registrar)
        runner.set_shutdown_requested(MagicMock(side_effect=[False, True]))
        with (
            patch("orca_auto.orca.orca_runner.signal.getsignal", side_effect=_getsignal),
            patch("orca_auto.orca.orca_runner.signal.signal", side_effect=_signal),
            patch.object(runner, "_retain_until_subprocess_tree_exits", side_effect=_cleanup),
            patch.object(runner, "_clear_process_record_if_group_gone", side_effect=_clear_record),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaises(WorkerShutdownInterrupt) as caught:
                    runner.run(inp)

        self.assertIs(type(caught.exception), WorkerShutdownInterrupt)
        self.assertEqual(
            events,
            [
                "admission_registered",
                "process_tree_reaped",
                "process_record_cleared",
                "admission_cleared",
            ],
        )

    @patch("orca_auto.orca.orca_runner.signal.signal")
    @patch("orca_auto.orca.orca_runner.signal.getsignal", return_value=signal.SIG_DFL)
    @patch("orca_auto.orca.orca_runner.subprocess.Popen")
    def test_sigint_cannot_reenter_process_record_initialization_cleanup(
        self,
        mock_popen: MagicMock,
        _mock_getsignal: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        events: list[str] = []

        def _registrar(running: object | None) -> None:
            events.append("admission_registered" if running is not None else "admission_cleared")

        def _terminate(_proc: object) -> bool:
            handler = _installed_signal_handler(mock_signal, signal.SIGINT)
            handler(signal.SIGINT, None)
            handler(signal.SIGINT, None)
            events.append("process_tree_reaped")
            return True

        runner = OrcaRunner("/opt/orca/orca")
        runner.set_running_job_registrar(_registrar)
        with (
            patch(
                "orca_auto.orca.orca_runner.write_orca_process_record",
                side_effect=RuntimeError("record failed"),
            ),
            patch.object(runner, "_terminate_subprocess_tree", side_effect=_terminate),
        ):
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "test.inp"
                inp.write_text("! Opt\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "record failed"):
                    runner.run(inp)

        self.assertEqual(
            events,
            ["admission_registered", "process_tree_reaped", "admission_cleared"],
        )
