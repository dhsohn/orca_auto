from __future__ import annotations

import hashlib
import logging
import os
import signal
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType, SimpleNamespace, TracebackType
from typing import Any

from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
    open_confined_log,
    require_confined_regular_file,
    thread_limited_env,
)
from orca_auto.core.engine_runner import confined_output_identity, executable_identity
from orca_auto.core.engine_scratch import (
    EngineScratchWorkspace,
    ScratchPublication,
    attach_scratch_provenance_to_exception,
    scratch_publication_provenance,
)
from orca_auto.core.queue.cancellable import (
    ProcessCleanupError,
    retain_process_ownership_until_exit,
)
from orca_auto.core.queue.processes import (
    ProcessGroupTerminationDeps,
    managed_process_group_has_exited,
    process_group_exists,
    terminate_process_group,
)

from .scratch import OrcaScratchPolicy

logger = logging.getLogger(__name__)


class WorkerShutdownInterrupt(KeyboardInterrupt):
    """Raised when a worker shutdown signal or request stops the current ORCA run."""


class ShutdownSignalGuard:
    """Record shutdown signals without raising asynchronously from their handler.

    A Python signal handler can run between any two bytecodes. Raising directly
    from it therefore leaves unavoidable gaps around handler installation,
    ``except`` entry, and ``finally`` entry where process-tree or admission cleanup
    can be skipped. The handler records only the first SIGTERM/SIGINT instead.
    ``OrcaRunner`` polls this state while waiting and keeps the handlers installed
    until all process and bookkeeping cleanup has finished.

    Capturing SIGINT too prevents a second Ctrl-C from unwinding the cleanup that
    the first one started. The runner preserves standalone Ctrl-C as
    ``KeyboardInterrupt`` while worker-managed signals become
    ``WorkerShutdownInterrupt``.
    """

    _MANAGED_SIGNALS = (signal.SIGTERM, signal.SIGINT)

    def __init__(self) -> None:
        self._previous_handlers: dict[int, Any] = {}
        self._installed_signals: list[int] = []
        self.received_signal: int | None = None

    def __enter__(self) -> ShutdownSignalGuard:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, self._MANAGED_SIGNALS)
        try:
            try:
                for signum in self._MANAGED_SIGNALS:
                    self._previous_handlers[signum] = signal.getsignal(signum)
                    # Record ownership before installation: a different signal can
                    # dispatch immediately after signal.signal() returns.
                    self._installed_signals.append(signum)
                    signal.signal(signum, self._handle_signal)
            except ValueError:
                # signal handlers can only be installed in the main thread
                self._restore_handlers()
            except BaseException:
                self._restore_handlers()
                raise
        finally:
            # A signal queued while only one handler was installed is delivered
            # only after both state-only handlers (or all previous handlers) are set.
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, self._MANAGED_SIGNALS)
        try:
            self._restore_handlers()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def _restore_handlers(self) -> None:
        # Restore SIGTERM first. Until SIGINT is restored last, Ctrl-C remains a
        # state-only event and cannot interrupt the restoration sequence.
        for signum in tuple(self._installed_signals):
            try:
                signal.signal(signum, self._previous_handlers[signum])
            except ValueError:
                logger.debug("failed to restore signal handler outside main thread")
        self._installed_signals.clear()

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        if self.received_signal is None:
            self.received_signal = signum

    @property
    def installed(self) -> bool:
        return len(self._installed_signals) == len(self._MANAGED_SIGNALS)


@dataclass
class RunResult:
    out_path: str
    return_code: int
    command: tuple[str, ...] = ()
    input_identity: dict[str, Any] = field(default_factory=dict)
    executable_identity: dict[str, Any] = field(default_factory=dict)
    output_identity: dict[str, Any] = field(default_factory=dict)
    execution_provenance: dict[str, Any] = field(default_factory=dict)
    scratch_provenance: dict[str, Any] = field(default_factory=dict)


class OrcaRunner:
    def __init__(self, orca_executable: str) -> None:
        self.orca_executable = orca_executable
        self._prepare_running_job: Callable[[], None] | None = None
        self._register_running_job: Callable[[Any | None], None] | None = None
        self._shutdown_requested: Callable[[], bool] | None = None
        self._bound_executable_identity: dict[str, Any] = {}
        self._bound_durable_directory_identity: tuple[int, int] | None = None
        self._scratch_policy: OrcaScratchPolicy | None = None

    def set_running_job_registrar(
        self,
        registrar: Callable[[Any | None], None],
        *,
        prepare: Callable[[], None],
    ) -> None:
        self._register_running_job = registrar
        self._prepare_running_job = prepare

    def set_shutdown_requested(self, callback: Callable[[], bool]) -> None:
        self._shutdown_requested = callback

    def set_executable_identity(self, identity: dict[str, Any]) -> None:
        self._bound_executable_identity = dict(identity)

    def set_scratch_policy(self, policy: OrcaScratchPolicy) -> None:
        self._scratch_policy = policy

    def set_durable_directory_identity(self, identity: dict[str, Any]) -> None:
        device = identity.get("device")
        inode = identity.get("inode")
        if type(device) is not int or device < 0 or type(inode) is not int or inode <= 0:
            raise ValueError("ORCA durable directory identity is invalid")
        self._bound_durable_directory_identity = (device, inode)

    def _open_pinned_executable(self) -> tuple[int, dict[str, Any]]:
        path = Path(self.orca_executable).expanduser().resolve()
        # A pathname can be replaced after snapshot verification.  Open
        # non-blocking so a substituted FIFO cannot stall the worker before
        # fstat() rejects it as a non-regular executable.
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"ORCA executable is not a regular file: {path}")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError(f"ORCA executable changed while it was pinned: {path}")
            os.lseek(descriptor, 0, os.SEEK_SET)
            observed = {
                "path": str(path),
                "sha256": digest.hexdigest(),
                "size_bytes": int(after.st_size),
            }
            if self._bound_executable_identity and observed != self._bound_executable_identity:
                raise ValueError("ORCA executable no longer matches its queued identity")
            return descriptor, observed
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_pinned_launch_gate() -> int:
        path = Path(__file__).resolve().with_name("launch_gate.py")
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"ORCA launch gate is not a regular file: {path}")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _cleanup_published_workspace(workspace: EngineScratchWorkspace) -> None:
        try:
            workspace.cleanup()
        except BaseException:  # noqa: BLE001
            logger.exception(
                "Published ORCA scratch workspace could not be removed; "
                "future scratch runs will remain fail-closed until it is inspected: %s",
                workspace.path,
            )

    def _terminate_subprocess_tree(self, proc: subprocess.Popen) -> bool:
        """Terminate the ORCA process group; True only when it is confirmed gone."""
        logger.warning("Terminating ORCA process tree (pid=%d)", proc.pid)
        return terminate_process_group(
            proc,
            graceful_timeout=3,
            kill_timeout=5,
            killpg_fn=os.killpg,
            sigterm=signal.SIGTERM,
            sigkill=signal.SIGKILL,
            deps=ProcessGroupTerminationDeps(
                logger=logger,
                process_group_exists=lambda pgid: process_group_exists(
                    pgid,
                    killpg_fn=os.killpg,
                ),
            ),
        )

    def _retain_until_subprocess_tree_exits(self, proc: subprocess.Popen) -> None:
        try:
            if self._terminate_subprocess_tree(proc):
                return
        except Exception:  # noqa: BLE001
            logger.exception(
                "ORCA process-group termination raised; retaining ownership until group exit"
            )
        retain_process_ownership_until_exit(
            proc,
            terminate_process=self._terminate_subprocess_tree,
        )

    @staticmethod
    def _write_interrupt_notice(handle: Any, message: str) -> None:
        """Append a diagnostic without allowing output I/O to block process cleanup."""
        try:
            handle.write(message)
            handle.flush()
        except (OSError, ValueError):
            logger.warning("Could not append the ORCA interruption notice", exc_info=True)

    @staticmethod
    def _open_output_log_at(directory_fd: int, name: str) -> Any:
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            os.close(descriptor)
            raise ValueError(f"ORCA output must be a private regular file: {name}")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.fdopen(descriptor, "w", encoding="utf-8")

    @staticmethod
    def _ensure_trailing_newline(path: Path) -> None:
        """Ensure a trailing newline so ORCA's Fortran parser reads the last line correctly."""
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            atomic_write_confined_bytes(
                path.parent,
                path,
                data + b"\n",
                label="ORCA normalized input",
            )

    def run(self, inp_path: Path) -> RunResult:
        if self._prepare_running_job is None or self._register_running_job is None:
            raise RuntimeError("ORCA execution requires managed admission callbacks")
        durable_input = require_confined_regular_file(
            inp_path.parent,
            inp_path,
            label="ORCA selected input",
        )
        if self._scratch_policy is None:
            return self._run_in_place(durable_input)
        workspace = EngineScratchWorkspace.create(
            self._scratch_policy,
            durable_input,
            expected_durable_dir_identity=self._bound_durable_directory_identity,
        )
        publication: ScratchPublication | None = None
        try:
            try:
                result = self._run_in_place(
                    workspace.scratch_input,
                    working_directory_fd=workspace.workspace_dir_fd,
                )
            except BaseException as exc:
                publication = workspace.publish()
                attach_scratch_provenance_to_exception(exc, publication)
                raise
            publication = workspace.publish()
            durable_out = durable_input.parent / Path(result.out_path).name
            result.out_path = str(durable_out)
            result.input_identity = executable_identity(durable_input)
            result.output_identity = confined_output_identity(durable_input.parent, durable_out)
            result.scratch_provenance = scratch_publication_provenance(publication)
            return result
        except BaseException as exc:
            if publication is None:
                logger.error(
                    "Retaining unpublished ORCA scratch workspace for recovery: %s",
                    workspace.path,
                )
            else:
                attach_scratch_provenance_to_exception(exc, publication)
            raise
        finally:
            if publication is not None:
                self._cleanup_published_workspace(workspace)
            workspace.close()

    def _run_in_place(
        self,
        inp_path: Path,
        *,
        working_directory_fd: int | None = None,
    ) -> RunResult:
        prepare_running_job = self._prepare_running_job
        register_running_job = self._register_running_job
        assert prepare_running_job is not None
        assert register_running_job is not None
        if working_directory_fd is None:
            inp = require_confined_regular_file(
                inp_path.parent,
                inp_path,
                label="ORCA selected input",
            )
        else:
            working_directory = Path("/proc/self/fd") / str(working_directory_fd)
            secure_input = working_directory / inp_path.name
            details = os.stat(inp_path.name, dir_fd=working_directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ValueError(f"ORCA selected input is unsafe: {inp_path.name}")
            inp = secure_input
        if working_directory_fd is None:
            self._ensure_trailing_newline(inp)
        out = inp.with_suffix(".out")
        cwd = str(inp.parent)

        command: list[str] = [self.orca_executable, inp.name]
        input_identity = executable_identity(inp)
        bound_executable_identity = dict(self._bound_executable_identity)
        logger.info("Running ORCA: %s in %s", command, cwd)

        return_code = 1
        output_handle = (
            open_confined_log(inp.parent, out, label="ORCA output")
            if working_directory_fd is None
            else self._open_output_log_at(working_directory_fd, out.name)
        )
        with output_handle as handle:
            with ShutdownSignalGuard() as shutdown_guard:

                def _raise_if_shutdown_requested() -> None:
                    received_signal = shutdown_guard.received_signal
                    if received_signal == signal.SIGINT and self._shutdown_requested is None:
                        raise KeyboardInterrupt
                    if received_signal is not None:
                        raise WorkerShutdownInterrupt
                    if self._shutdown_requested is not None and self._shutdown_requested():
                        raise WorkerShutdownInterrupt

                proc: subprocess.Popen | None = None
                admission_registered = False
                process_start_attempted = False
                try:
                    prepare_running_job()
                    _raise_if_shutdown_requested()
                    process_start_attempted = True
                    executable_fd, observed_executable_identity = self._open_pinned_executable()
                    if not bound_executable_identity:
                        bound_executable_identity = observed_executable_identity
                    launch_gate_fd = -1
                    try:
                        launch_gate_fd = self._open_pinned_launch_gate()
                        launch_command = [
                            "/proc/self/exe",
                            f"/proc/self/fd/{launch_gate_fd}",
                            str(launch_gate_fd),
                            self.orca_executable,
                            str(executable_fd),
                            inp.name,
                        ]
                        popen_kwargs: dict[str, Any] = {
                            "cwd": cwd,
                            "stdin": subprocess.PIPE,
                            "stdout": handle,
                            "stderr": subprocess.STDOUT,
                            "text": True,
                            "start_new_session": True,
                            # ORCA parallelizes across %pal MPI ranks, not OpenMP,
                            # so pin the thread env to 1: each rank stays
                            # single-threaded and N ranks do not each spawn N
                            # BLAS/OMP threads (N^2 oversubscription). The %pal
                            # count in the input keeps ORCA's real parallelism.
                            # xTB/CREST already pin threads through the
                            # shared launcher; ORCA's bespoke launch-gate path
                            # otherwise inherits the worker env unpinned. The env
                            # flows through the gate's execve into ORCA.
                            "env": thread_limited_env(dict(os.environ), 1),
                        }
                        inherited_fds = [launch_gate_fd, executable_fd]
                        if working_directory_fd is not None:
                            inherited_fds.append(working_directory_fd)
                        popen_kwargs["pass_fds"] = tuple(inherited_fds)
                        proc = subprocess.Popen(launch_command, **popen_kwargs)
                    finally:
                        os.close(executable_fd)
                        if launch_gate_fd >= 0:
                            os.close(launch_gate_fd)
                    register_running_job(SimpleNamespace(process=proc))
                    admission_registered = True
                    if proc.stdin is None:
                        raise RuntimeError("ORCA launch gate has no release pipe")
                    proc.stdin.write("1")
                    proc.stdin.flush()
                    proc.stdin.close()
                except BaseException as exc:
                    if proc is None:
                        if not process_start_attempted or isinstance(exc, Exception):
                            register_running_job(None)
                        raise
                    cleanup_error: ProcessCleanupError | None = None
                    try:
                        terminated = self._terminate_subprocess_tree(proc)
                        process_exited = proc.poll() is not None
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_error = ProcessCleanupError(
                            "Failed to clean up ORCA after managed launch initialization failed: "
                            f"{cleanup_exc}"
                        )
                        terminated = False
                        process_exited = False
                    if not terminated or not process_exited:
                        cleanup_error = cleanup_error or ProcessCleanupError(
                            "Failed to clean up ORCA after managed launch initialization failed"
                        )
                        retain_process_ownership_until_exit(
                            proc,
                            terminate_process=self._terminate_subprocess_tree,
                        )
                    if terminated and process_exited:
                        try:
                            register_running_job(None)
                        except Exception as admission_exc:  # noqa: BLE001
                            cleanup_error = cleanup_error or ProcessCleanupError(
                                "Failed to clear ORCA admission process record after cleanup"
                            )
                            cleanup_error.__cause__ = admission_exc
                    if cleanup_error is not None:
                        raise cleanup_error from exc
                    raise
                assert proc is not None

                def _owned_process_group_exists() -> bool:
                    return not managed_process_group_has_exited(proc, killpg_fn=os.killpg)

                try:
                    while True:
                        _raise_if_shutdown_requested()
                        try:
                            return_code = proc.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            continue
                        break
                except WorkerShutdownInterrupt:
                    self._retain_until_subprocess_tree_exits(proc)
                    self._write_interrupt_notice(
                        handle,
                        "\n[orca_auto] interrupted by worker shutdown; "
                        "terminated ORCA process tree\n",
                    )
                    raise
                except KeyboardInterrupt:
                    self._retain_until_subprocess_tree_exits(proc)
                    self._write_interrupt_notice(
                        handle,
                        "\n[orca_auto] interrupted by user; terminated ORCA process tree\n",
                    )
                    raise
                except BaseException:
                    # A failed wait/callback can race the leader's exit. Only
                    # terminate a reaped group after ruling out PID reuse.
                    if _owned_process_group_exists():
                        self._retain_until_subprocess_tree_exits(proc)
                    raise
                else:
                    if _owned_process_group_exists():
                        logger.warning(
                            "ORCA launcher exited while its process group remained active; "
                            "retaining ownership until the group is gone"
                        )
                        self._retain_until_subprocess_tree_exits(proc)
                    # The launcher and any lingering children are already gone,
                    # so a signal received during their cleanup can propagate
                    # without entering the termination path a second time.
                    _raise_if_shutdown_requested()
                finally:
                    # The state-only handlers remain installed through bookkeeping,
                    # so neither repeated SIGTERM nor Ctrl-C can strand ownership.
                    if admission_registered:
                        register_running_job(None)
            # A signal received during normal process-tree/bookkeeping cleanup is
            # delivered only after ownership has been released. A worker's restored
            # handler may also have set the polling callback during handler restore.
            _raise_if_shutdown_requested()
        if executable_identity(inp) != input_identity:
            raise RuntimeError(f"ORCA execution input changed while it was running: {inp}")
        output_identity = (
            confined_output_identity(inp.parent, out)
            if working_directory_fd is None
            else executable_identity(out)
        )
        return RunResult(
            out_path=str(out),
            return_code=return_code,
            command=tuple(command),
            input_identity=input_identity,
            executable_identity=bound_executable_identity,
            output_identity=output_identity,
        )
