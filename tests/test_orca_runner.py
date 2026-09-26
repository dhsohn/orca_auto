import errno
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from orca_auto.core.engine_scratch import (
    EngineScratchError,
    EngineScratchWorkspace,
    scratch_provenance_from_exception,
)
from orca_auto.core.engine_scratch import _policy as policy_mod
from orca_auto.core.engine_scratch import _workspace as workspace_mod
from orca_auto.core.queue.cancellable import ProcessCleanupError
from orca_auto.orca import orca_runner
from orca_auto.orca.orca_runner import (
    OrcaRunner,
    ShutdownSignalGuard,
    WorkerShutdownInterrupt,
)
from orca_auto.orca.scratch import OrcaScratchPolicy

_TEST_EXECUTABLE = "/opt/orca/orca"


def _installed_signal_handler(
    mock_signal: MagicMock,
    signum: int = signal.SIGTERM,
) -> Callable[[int, object], None]:
    for signal_call in mock_signal.call_args_list:
        handler = signal_call.args[1]
        if signal_call.args[0] == signum and callable(handler):
            return handler
    raise AssertionError(f"no installed handler found for signal {signum}")


def _managed_runner(executable: str = _TEST_EXECUTABLE) -> OrcaRunner:
    runner = OrcaRunner(executable)
    runner.set_running_job_registrar(lambda _running: None, prepare=lambda: None)
    return runner


def _mock_process(*, pid: int = 99999, poll: int | None = None, wait: int | None = None) -> Any:
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = poll
    if wait is not None:
        proc.wait.return_value = wait
    return proc


def _admission_events(events: list[str]) -> Callable[[object | None], None]:
    def registrar(running: object | None) -> None:
        events.append("admission_registered" if running is not None else "admission_cleared")

    return registrar


@pytest.fixture(autouse=True)
def pinned_test_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``/opt/orca/orca`` resolve to ``/bin/true`` without touching real paths."""

    original_open = OrcaRunner._open_pinned_executable

    def open_test_executable(runner: OrcaRunner) -> Any:
        if runner.orca_executable == _TEST_EXECUTABLE:
            descriptor = os.open("/bin/true", os.O_RDONLY)
            details = os.fstat(descriptor)
            return descriptor, {
                "path": runner.orca_executable,
                "sha256": "test-double",
                "size_bytes": int(details.st_size),
            }
        return original_open(runner)

    monkeypatch.setattr(OrcaRunner, "_open_pinned_executable", open_test_executable)


@pytest.fixture
def inp(tmp_path: Path) -> Path:
    path = tmp_path / "test.inp"
    path.write_text("! Opt\n", encoding="utf-8")
    return path


@pytest.fixture
def mock_popen(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``subprocess.Popen`` inside the runner module, as a MagicMock."""

    popen = MagicMock()
    monkeypatch.setattr(orca_runner.subprocess, "Popen", popen)
    return popen


@pytest.fixture
def mock_signal(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``signal.signal`` inside the runner module, with ``getsignal`` reporting SIG_DFL."""

    installer = MagicMock()
    monkeypatch.setattr(orca_runner.signal, "signal", installer)
    monkeypatch.setattr(orca_runner.signal, "getsignal", MagicMock(return_value=signal.SIG_DFL))
    return installer


@pytest.fixture
def ram_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``/dev/shm`` with unlimited memory; returns the scratch root parent."""

    fake_shm = tmp_path / "shm"
    fake_shm.mkdir()
    monkeypatch.setattr(policy_mod, "_SCRATCH_ROOT_PARENT", fake_shm)
    monkeypatch.setattr(workspace_mod, "_linux_available_memory_bytes", lambda: 2**63)
    return fake_shm


def _scratch_policy(fake_shm: Path) -> OrcaScratchPolicy:
    return OrcaScratchPolicy(root=fake_shm / "orca_auto", min_free_bytes=1, max_task_memory_bytes=1)


@pytest.fixture
def durable_inp(tmp_path: Path) -> Path:
    durable = tmp_path / "durable"
    durable.mkdir()
    path = durable / "test.inp"
    path.write_text("! SP\n", encoding="utf-8")
    return path


# -- command construction ---------------------------------------------------


def test_open_pinned_executable_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    executable = tmp_path / "fake-orca"
    os.mkfifo(executable)

    runner = _managed_runner(str(executable))
    with pytest.raises(ValueError, match="not a regular file"):
        runner._open_pinned_executable()


def test_command_uses_linux_binary(mock_popen: MagicMock, inp: Path) -> None:
    mock_popen.return_value = _mock_process(wait=0)

    result = _managed_runner().run(inp)

    args, kwargs = mock_popen.call_args
    command = args[0]
    assert command[0] == sys.executable
    assert kwargs["executable"] == "/proc/self/exe"
    assert command[1].startswith("/proc/self/fd/")
    launch_gate_fd = int(command[1].removeprefix("/proc/self/fd/"))
    assert int(command[2]) == launch_gate_fd
    assert command[3] == _TEST_EXECUTABLE
    executable_fd = int(command[4])
    assert executable_fd >= 3
    assert command[5] == "test.inp"
    assert launch_gate_fd in kwargs["pass_fds"]
    assert executable_fd in kwargs["pass_fds"]
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["start_new_session"]
    assert result.command == (_TEST_EXECUTABLE, "test.inp")
    assert result.input_identity["path"] == str(inp)
    assert result.input_identity["size_bytes"] == len(b"! Opt\n")


def test_launch_pins_thread_env_to_one(mock_popen: MagicMock, inp: Path) -> None:
    # ORCA parallelizes across %pal MPI ranks, so the launch env pins the
    # OpenMP/BLAS thread count to 1 (each rank single-threaded), matching the
    # discipline the other engines apply and preventing N^2 oversubscription.
    mock_popen.return_value = _mock_process(wait=0)

    _managed_runner().run(inp)

    _, kwargs = mock_popen.call_args
    env = kwargs["env"]
    for var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        assert env[var] == "1"
    # The rest of the inherited environment is preserved, not replaced.
    assert "PATH" in env


def test_ram_scratch_publishes_results(
    ram_scratch: Path, durable_inp: Path, make_fake_orca: Callable[..., Path]
) -> None:
    executable = make_fake_orca(
        "#!/bin/sh\n"
        "stem=${1%.inp}\n"
        'printf checkpoint > "$stem.gbw"\n'
        'printf scratch > "$stem.EIJ.tmp"\n'
        "printf 'ORCA TERMINATED NORMALLY\\n'\n"
    )
    durable = durable_inp.parent

    runner = _managed_runner(str(executable))
    runner.set_scratch_policy(_scratch_policy(ram_scratch))
    result = runner.run(durable_inp)

    assert result.out_path == str(durable / "test.out")
    assert result.input_identity["path"] == str(durable_inp)
    assert (durable / "test.gbw").is_file()
    assert not (durable / "test.EIJ.tmp").exists()
    assert result.scratch_provenance["used"]
    assert result.scratch_provenance["omitted_transient_files"] == ["test.EIJ.tmp"]


def test_ram_scratch_normalizes_only_the_private_input_copy(
    ram_scratch: Path, durable_inp: Path, make_fake_orca: Callable[..., Path]
) -> None:
    executable = make_fake_orca(
        "#!/bin/sh\n"
        'test "$(tail -c 1 "$1" | wc -l)" -eq 1 || exit 9\n'
        "printf 'ORCA TERMINATED NORMALLY\\n'\n"
    )
    durable_inp.write_bytes(b"! SP")

    runner = _managed_runner(str(executable))
    runner.set_scratch_policy(_scratch_policy(ram_scratch))
    result = runner.run(durable_inp)

    assert result.return_code == 0
    assert durable_inp.read_bytes() == b"! SP"
    assert "test.out" in result.scratch_provenance["published_files"]


def test_ram_scratch_publishes_checkpoint_before_shutdown_propagates(
    ram_scratch: Path, durable_inp: Path, make_fake_orca: Callable[..., Path]
) -> None:
    executable = make_fake_orca(
        "#!/bin/sh\n"
        "stem=${1%.inp}\n"
        'printf checkpoint > "$stem.gbw"\n'
        'printf scratch > "$stem.EIJ.tmp"\n'
        "sleep 30\n",
        name="slow_orca",
    )
    durable = durable_inp.parent
    shutdown_checks = 0

    def shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        if shutdown_checks == 1:
            return False
        time.sleep(0.1)
        return True

    runner = _managed_runner(str(executable))
    runner.set_shutdown_requested(shutdown_requested)
    runner.set_scratch_policy(_scratch_policy(ram_scratch))
    with pytest.raises(WorkerShutdownInterrupt) as caught:
        runner.run(durable_inp)

    assert (durable / "test.gbw").read_bytes() == b"checkpoint"
    assert "interrupted by worker shutdown" in (durable / "test.out").read_text()
    assert not (durable / "test.EIJ.tmp").exists()
    assert scratch_provenance_from_exception(caught.value)["published_files"] == [
        "test.gbw",
        "test.out",
    ]
    assert not any(
        path.name.startswith("attempt-") for path in (ram_scratch / "orca_auto").iterdir()
    )


def test_ram_scratch_executes_through_pinned_workspace_after_root_replacement(
    ram_scratch: Path,
    durable_inp: Path,
    make_fake_orca: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = make_fake_orca("#!/bin/sh\nprintf 'ORCA TERMINATED NORMALLY\\n'\n")
    durable = durable_inp.parent
    moved_root = ram_scratch / "orca_auto-moved"

    runner = _managed_runner(str(executable))
    original_create = EngineScratchWorkspace.create

    def replace_root_after_create(*args: Any, **kwargs: Any) -> Any:
        workspace = original_create(*args, **kwargs)
        workspace.policy.root.rename(moved_root)
        workspace.policy.root.mkdir()
        replacement = workspace.policy.root / workspace.path.name
        replacement.mkdir()
        (replacement / durable_inp.name).write_bytes(durable_inp.read_bytes())
        return workspace

    monkeypatch.setattr(EngineScratchWorkspace, "create", replace_root_after_create)
    runner.set_scratch_policy(_scratch_policy(ram_scratch))
    with pytest.raises(EngineScratchError, match="workspace pathname identity changed"):
        runner.run(durable_inp)

    moved_workspace = next(
        path for path in moved_root.iterdir() if path.name.startswith("attempt-")
    )
    assert "ORCA TERMINATED NORMALLY" in (moved_workspace / "test.out").read_text(encoding="utf-8")
    replacement_workspace = ram_scratch / "orca_auto" / moved_workspace.name
    assert not (replacement_workspace / "test.out").exists()
    assert not (durable / "test.out").exists()


# -- termination ------------------------------------------------------------


def test_terminate_noop_when_process_already_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _managed_runner()
    killpg = MagicMock()
    monkeypatch.setattr(orca_runner.os, "killpg", killpg)
    monkeypatch.setattr(orca_runner, "process_group_exists", lambda *_args, **_kwargs: False)

    assert runner._terminate_subprocess_tree(_mock_process(poll=0))
    killpg.assert_not_called()


def test_terminate_sends_sigterm_and_sigkill_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _managed_runner()
    killpg = MagicMock()
    monkeypatch.setattr(orca_runner.os, "killpg", killpg)
    mock_proc = _mock_process()
    mock_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="orca", timeout=3),
        subprocess.TimeoutExpired(cmd="orca", timeout=5),
    ]

    result = runner._terminate_subprocess_tree(mock_proc)

    assert not result  # never exited -> termination not confirmed
    assert killpg.mock_calls == [call(99999, signal.SIGTERM), call(99999, signal.SIGKILL)]


def test_run_sigterm_terminates_orca_tree(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_proc = _mock_process()

    def _wait(*_args: object, **_kwargs: object) -> int:
        _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
        raise subprocess.TimeoutExpired(cmd="orca", timeout=0.2)

    mock_proc.wait.side_effect = _wait
    mock_popen.return_value = mock_proc
    terminated: list[object] = []

    def _terminate(proc: object) -> bool:
        terminated.append(proc)
        return True

    runner = _managed_runner()
    monkeypatch.setattr(runner, "_terminate_subprocess_tree", _terminate)
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    assert terminated == [mock_proc]
    assert mock_signal.call_args_list[0].args[0] == signal.SIGTERM
    assert call(signal.SIGTERM, signal.SIG_DFL) in mock_signal.call_args_list
    assert call(signal.SIGINT, signal.SIG_DFL) in mock_signal.call_args_list


# -- admission lifecycle ----------------------------------------------------


def test_unmanaged_runner_refuses_before_process_start(mock_popen: MagicMock, inp: Path) -> None:
    runner = OrcaRunner(_TEST_EXECUTABLE)

    with pytest.raises(RuntimeError, match="requires managed admission callbacks"):
        runner.run(inp)

    mock_popen.assert_not_called()


def test_admission_identity_is_published_before_gate_release(
    mock_popen: MagicMock, inp: Path
) -> None:
    events: list[str] = []
    mock_proc = _mock_process(poll=0, wait=0)
    mock_proc.stdin.write.side_effect = lambda _value: events.append("gate_released")
    mock_popen.return_value = mock_proc

    runner = OrcaRunner(_TEST_EXECUTABLE)
    runner.set_running_job_registrar(
        _admission_events(events), prepare=lambda: events.append("admission_prepared")
    )
    runner.run(inp)

    assert events == [
        "admission_prepared",
        "admission_registered",
        "gate_released",
        "admission_cleared",
    ]


def test_gate_release_failure_cleans_process_before_admission(
    mock_popen: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    mock_proc = _mock_process(poll=0)
    mock_proc.stdin.write.side_effect = OSError("release failed")
    mock_popen.return_value = mock_proc

    runner = OrcaRunner(_TEST_EXECUTABLE)
    runner.set_running_job_registrar(
        _admission_events(events), prepare=lambda: events.append("admission_prepared")
    )

    def terminate(_proc: object) -> bool:
        events.append("process_tree_reaped")
        return True

    monkeypatch.setattr(runner, "_terminate_subprocess_tree", terminate)
    with pytest.raises(OSError, match="release failed"):
        runner.run(inp)

    assert events == [
        "admission_prepared",
        "admission_registered",
        "process_tree_reaped",
        "admission_cleared",
    ]


def test_admission_publish_failure_retains_fence_when_cleanup_is_unconfirmed(
    mock_popen: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    mock_popen.return_value = _mock_process()

    def registrar(running: object | None) -> None:
        if running is not None:
            events.append("admission_publish_failed")
            raise RuntimeError("publish failed")
        events.append("admission_cleared")

    runner = OrcaRunner(_TEST_EXECUTABLE)
    runner.set_running_job_registrar(registrar, prepare=lambda: events.append("prepared"))
    monkeypatch.setattr(runner, "_terminate_subprocess_tree", lambda _proc: False)
    monkeypatch.setattr(
        orca_runner,
        "retain_process_ownership_until_exit",
        lambda *_args, **_kwargs: events.append("ownership_retained"),
    )
    with pytest.raises(ProcessCleanupError):
        runner.run(inp)

    assert events == ["prepared", "admission_publish_failed", "ownership_retained"]


# -- shutdown signal guard --------------------------------------------------
# Shutdown signals are state-only until the runner reaches a safe boundary.


def test_guard_records_only_the_first_signal_without_raising(mock_signal: MagicMock) -> None:
    protected_work_completed: list[bool] = []
    with ShutdownSignalGuard() as guard:
        assert guard.installed
        guard._handle_signal(signal.SIGTERM, None)
        guard._handle_signal(signal.SIGINT, None)
        protected_work_completed.append(True)
        assert guard.received_signal == signal.SIGTERM

    assert protected_work_completed == [True]


def test_guard_does_not_mask_cleanup_failure_after_signal(mock_signal: MagicMock) -> None:
    with pytest.raises(RuntimeError, match="cleanup failed"), ShutdownSignalGuard() as guard:
        guard._handle_signal(signal.SIGTERM, None)
        raise RuntimeError("cleanup failed")


def test_guard_survives_non_main_thread_install_failure(mock_signal: MagicMock) -> None:
    mock_signal.side_effect = ValueError("not main thread")
    with ShutdownSignalGuard() as guard:
        assert not guard.installed
        guard._handle_signal(signal.SIGTERM, None)


def test_sigterm_during_cleanup_does_not_abort_process_tree_termination(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces TS8_wf/03_ts_guess: cancel SIGTERM lands while cleanup runs.

    Before the guard, the second signal escaped _retain_until_subprocess_tree_exits
    mid-way, leaving the reaped ORCA leader with a live process group -- reported as
    runner_exception rather than a cancellation.
    """
    mock_proc = _mock_process()

    def _wait(*_args: object, **_kwargs: object) -> int:
        _installed_signal_handler(mock_signal)(
            signal.SIGTERM, None
        )  # first SIGTERM: marks the wait
        raise subprocess.TimeoutExpired(cmd="orca", timeout=0.2)

    mock_proc.wait.side_effect = _wait
    mock_popen.return_value = mock_proc

    cleanup_completed: list[bool] = []

    def _cleanup(proc: object) -> None:
        # a supervisor SIGTERM lands in the middle of terminating the tree
        _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
        cleanup_completed.append(True)

    runner = _managed_runner()
    monkeypatch.setattr(runner, "_retain_until_subprocess_tree_exits", _cleanup)
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    assert cleanup_completed == [True]
    assert call(signal.SIGTERM, signal.SIG_DFL) in mock_signal.call_args_list
    assert call(signal.SIGINT, signal.SIG_DFL) in mock_signal.call_args_list


def test_sigterm_during_post_exit_retention_is_raised_after_bookkeeping(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first SIGTERM during lingering-child cleanup must remain a cancellation."""
    mock_popen.return_value = _mock_process(poll=0, wait=0)
    events: list[str] = []

    def _retain(_proc: object) -> None:
        _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
        events.append("process_tree_reaped")

    runner = _managed_runner()
    runner.set_running_job_registrar(_admission_events(events), prepare=lambda: None)
    monkeypatch.setattr(
        orca_runner, "managed_process_group_has_exited", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(runner, "_retain_until_subprocess_tree_exits", _retain)
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    assert events == ["admission_registered", "process_tree_reaped", "admission_cleared"]


def test_polled_shutdown_keeps_signals_state_only_during_cleanup(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_popen.return_value = _mock_process()
    cleanup_completed: list[bool] = []

    def _cleanup(_proc: object) -> None:
        _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
        cleanup_completed.append(True)

    runner = _managed_runner()
    runner.set_shutdown_requested(MagicMock(side_effect=[False, True]))
    monkeypatch.setattr(runner, "_retain_until_subprocess_tree_exits", _cleanup)
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    assert cleanup_completed == [True]


def test_repeated_sigint_during_cleanup_preserves_keyboard_interrupt(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_proc = _mock_process()

    def _wait(*_args: object, **_kwargs: object) -> int:
        _installed_signal_handler(mock_signal, signal.SIGINT)(signal.SIGINT, None)
        raise subprocess.TimeoutExpired(cmd="orca", timeout=0.2)

    mock_proc.wait.side_effect = _wait
    mock_popen.return_value = mock_proc
    cleanup_completed: list[bool] = []

    def _cleanup(_proc: object) -> None:
        _installed_signal_handler(mock_signal, signal.SIGINT)(signal.SIGINT, None)
        cleanup_completed.append(True)

    runner = _managed_runner()
    monkeypatch.setattr(runner, "_retain_until_subprocess_tree_exits", _cleanup)
    with pytest.raises(KeyboardInterrupt) as caught:
        runner.run(inp)

    assert type(caught.value) is KeyboardInterrupt
    assert cleanup_completed == [True]


def test_signal_delivered_during_handler_install_prevents_process_start(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_popen.return_value = _mock_process()
    delivered = False

    def _install(signum: int, handler: object) -> None:
        nonlocal delivered
        if signum == signal.SIGTERM and callable(handler) and not delivered:
            delivered = True
            handler(signal.SIGTERM, None)

    mock_signal.side_effect = _install
    events: list[str] = []

    runner = _managed_runner()
    runner.set_running_job_registrar(_admission_events(events), prepare=lambda: None)
    monkeypatch.setattr(
        runner,
        "_retain_until_subprocess_tree_exits",
        lambda _proc: events.append("process_tree_reaped"),
    )
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    mock_popen.assert_not_called()
    assert events == ["admission_cleared"]


def test_sigterm_during_bookkeeping_is_raised_after_admission_release(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path
) -> None:
    mock_popen.return_value = _mock_process(poll=0, wait=0)
    events: list[str] = []

    def _registrar(running: object | None) -> None:
        if running is None:
            _installed_signal_handler(mock_signal)(signal.SIGTERM, None)
            events.append("admission_cleared")
        else:
            events.append("admission_registered")

    runner = _managed_runner()
    runner.set_running_job_registrar(_registrar, prepare=lambda: None)
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    assert events == ["admission_registered", "admission_cleared"]


def test_interrupt_notice_failure_cannot_preempt_process_cleanup(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_popen.return_value = _mock_process()
    events: list[str] = []

    def _failing_notice(_handle: object, message: str) -> None:
        failing_handle = MagicMock()
        failing_handle.write.side_effect = OSError(errno.ENOSPC, "disk full")
        OrcaRunner._write_interrupt_notice(failing_handle, message)
        events.append("notice_attempted")

    runner = _managed_runner()
    runner.set_shutdown_requested(MagicMock(side_effect=[False, True]))
    monkeypatch.setattr(
        runner,
        "_retain_until_subprocess_tree_exits",
        lambda _proc: events.append("process_tree_reaped"),
    )
    monkeypatch.setattr(runner, "_write_interrupt_notice", _failing_notice)
    with pytest.raises(WorkerShutdownInterrupt):
        runner.run(inp)

    assert events == ["process_tree_reaped", "notice_attempted"]


@pytest.fixture
def blocked_signal_restore() -> Iterator[None]:
    """Restore SIGTERM/SIGINT handlers with both signals masked, then restore the mask."""

    original_signal = signal.signal
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            for signum, handler in previous_handlers.items():
                original_signal(signum, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)


def test_guard_blocks_cross_signal_until_both_handlers_are_installed(
    blocked_signal_restore: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_signal = signal.signal
    delivered = False

    def _install_with_pending_sigint(signum: int, handler: Any) -> Any:
        nonlocal delivered
        previous = original_signal(signum, handler)
        if signum == signal.SIGTERM and callable(handler) and not delivered:
            delivered = True
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        return previous

    with monkeypatch.context() as patched:
        patched.setattr(orca_runner.signal, "signal", _install_with_pending_sigint)
        with ShutdownSignalGuard() as guard:
            assert guard.received_signal == signal.SIGINT


def test_callback_failure_terminates_live_process_before_bookkeeping(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_popen.return_value = _mock_process(poll=0)
    events: list[str] = []

    runner = _managed_runner()
    runner.set_running_job_registrar(_admission_events(events), prepare=lambda: None)
    runner.set_shutdown_requested(MagicMock(side_effect=[False, RuntimeError("poll failed")]))
    monkeypatch.setattr(
        orca_runner, "managed_process_group_has_exited", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        runner,
        "_retain_until_subprocess_tree_exits",
        lambda _proc: events.append("process_tree_reaped"),
    )
    with pytest.raises(RuntimeError, match="poll failed"):
        runner.run(inp)

    assert events == ["admission_registered", "process_tree_reaped", "admission_cleared"]


def test_signal_handlers_remain_active_through_cleanup_and_bookkeeping(
    mock_popen: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_popen.return_value = _mock_process()
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
        if running is None:
            _dispatch_sigint()
            events.append("admission_cleared")
        else:
            events.append("admission_registered")

    def _cleanup(_proc: object) -> None:
        _dispatch_sigint()
        events.append("process_tree_reaped")

    runner = _managed_runner()
    runner.set_running_job_registrar(_registrar, prepare=lambda: None)
    runner.set_shutdown_requested(MagicMock(side_effect=[False, True]))
    monkeypatch.setattr(orca_runner.signal, "getsignal", _getsignal)
    monkeypatch.setattr(orca_runner.signal, "signal", _signal)
    monkeypatch.setattr(runner, "_retain_until_subprocess_tree_exits", _cleanup)
    with pytest.raises(WorkerShutdownInterrupt) as caught:
        runner.run(inp)

    assert type(caught.value) is WorkerShutdownInterrupt
    assert events == ["admission_registered", "process_tree_reaped", "admission_cleared"]


def test_sigint_cannot_reenter_admission_initialization_cleanup(
    mock_popen: MagicMock, mock_signal: MagicMock, inp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_popen.return_value = _mock_process(poll=0)
    events: list[str] = []

    def _registrar(running: object | None) -> None:
        if running is not None:
            events.append("admission_registration_failed")
            raise RuntimeError("record failed")
        events.append("admission_cleared")

    def _terminate(_proc: object) -> bool:
        handler = _installed_signal_handler(mock_signal, signal.SIGINT)
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        events.append("process_tree_reaped")
        return True

    runner = _managed_runner()
    runner.set_running_job_registrar(_registrar, prepare=lambda: None)
    monkeypatch.setattr(runner, "_terminate_subprocess_tree", _terminate)
    with pytest.raises(RuntimeError, match="record failed"):
        runner.run(inp)

    assert events == ["admission_registration_failed", "process_tree_reaped", "admission_cleared"]
