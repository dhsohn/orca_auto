from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, SimpleNamespace, TracebackType
from typing import Any

from orca_auto.core.queue.cancellable import (
    ProcessCleanupError,
    retain_process_ownership_until_exit,
)
from orca_auto.core.queue.processes import ProcessGroupTerminationDeps, terminate_process_group

from .orca_process import (
    OrcaProcessRecoveryError,
    clear_orca_process_record,
    clear_orca_process_record_snapshot,
    orca_process_record_snapshot_from_exception,
    process_group_is_alive,
    write_orca_process_record,
)

logger = logging.getLogger(__name__)


def _reaped_pid_was_reused(pid: int) -> bool:
    """Return definite reuse after Popen.wait/poll reaped the original leader."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise OrcaProcessRecoveryError(
            f"Cannot verify reaped ORCA process identity for pid={pid}"
        ) from exc
    return True


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
    def signalled(self) -> bool:
        return self.received_signal is not None

    @property
    def installed(self) -> bool:
        return len(self._installed_signals) == len(self._MANAGED_SIGNALS)


@dataclass
class RunResult:
    out_path: str
    return_code: int


class OrcaRunner:
    def __init__(self, orca_executable: str) -> None:
        self.orca_executable = orca_executable
        self._prepare_running_job: Callable[[], None] | None = None
        self._register_running_job: Callable[[Any | None], None] | None = None
        self._shutdown_requested: Callable[[], bool] | None = None

    def set_running_job_registrar(
        self,
        registrar: Callable[[Any | None], None],
        *,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        self._register_running_job = registrar
        self._prepare_running_job = prepare

    def set_shutdown_requested(self, callback: Callable[[], bool]) -> None:
        self._shutdown_requested = callback

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
                process_group_exists=lambda pgid: process_group_is_alive(
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
    def _ensure_trailing_newline(path: Path) -> None:
        """Ensure a trailing newline so ORCA's Fortran parser reads the last line correctly."""
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            with path.open("ab") as fh:
                fh.write(b"\n")

    def run(self, inp_path: Path) -> RunResult:
        inp = inp_path.resolve()
        self._ensure_trailing_newline(inp)
        out = inp.with_suffix(".out")
        cwd = str(inp.parent)

        command: list[str] = [self.orca_executable, inp.name]
        logger.info("Running ORCA: %s in %s", command, cwd)

        return_code = 1
        with out.open("w", encoding="utf-8") as handle:
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
                    if self._prepare_running_job is not None:
                        self._prepare_running_job()
                    _raise_if_shutdown_requested()
                    process_start_attempted = True
                    proc = subprocess.Popen(
                        command,
                        cwd=cwd,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                    )
                    if self._register_running_job is not None:
                        self._register_running_job(SimpleNamespace(process=proc))
                        admission_registered = True
                    process_record = write_orca_process_record(
                        inp_path=inp,
                        out_path=out,
                        pid=proc.pid,
                    )
                except BaseException as exc:
                    if proc is None:
                        if (
                            not process_start_attempted or isinstance(exc, Exception)
                        ) and self._register_running_job is not None:
                            self._register_running_job(None)
                        raise
                    cleanup_error: ProcessCleanupError | None = None
                    try:
                        terminated = self._terminate_subprocess_tree(proc)
                        process_exited = proc.poll() is not None
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_error = ProcessCleanupError(
                            "Failed to clean up ORCA after process-record initialization failed: "
                            f"{cleanup_exc}"
                        )
                        terminated = False
                        process_exited = False
                    if not terminated or not process_exited:
                        cleanup_error = cleanup_error or ProcessCleanupError(
                            "Failed to clean up ORCA after process-record initialization failed"
                        )
                        retain_process_ownership_until_exit(
                            proc,
                            terminate_process=self._terminate_subprocess_tree,
                        )
                    failed_record = orca_process_record_snapshot_from_exception(exc)
                    if failed_record is not None:
                        clear_orca_process_record_snapshot(
                            inp.parent,
                            failed_record,
                            pid=proc.pid,
                        )
                    if self._register_running_job is not None:
                        try:
                            self._register_running_job(None)
                        except Exception as admission_exc:  # noqa: BLE001
                            cleanup_error = cleanup_error or ProcessCleanupError(
                                "Failed to clear ORCA admission process record after cleanup"
                            )
                            cleanup_error.__cause__ = admission_exc
                    if cleanup_error is not None:
                        raise cleanup_error from exc
                    raise
                assert proc is not None

                def _owned_process_group_is_alive() -> bool:
                    if proc.poll() is None:
                        return True
                    return not _reaped_pid_was_reused(proc.pid) and process_group_is_alive(
                        proc.pid,
                        killpg_fn=os.killpg,
                    )

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
                    if _owned_process_group_is_alive():
                        self._retain_until_subprocess_tree_exits(proc)
                    raise
                else:
                    if _owned_process_group_is_alive():
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
                    self._clear_process_record_if_group_gone(inp.parent, proc, process_record)
                    if admission_registered and self._register_running_job is not None:
                        self._register_running_job(None)
            # A signal received during normal process-tree/bookkeeping cleanup is
            # delivered only after ownership has been released. A worker's restored
            # handler may also have set the polling callback during handler restore.
            _raise_if_shutdown_requested()
        return RunResult(out_path=str(out), return_code=return_code)

    @staticmethod
    def _clear_process_record_if_group_gone(
        reaction_dir: Path,
        proc: subprocess.Popen,
        process_record: dict[str, object],
    ) -> None:
        """Clear the process record only when the whole ORCA group has exited.

        ``terminate_process_group`` waits on the group LEADER, so its success
        does not prove PAL/child processes in the same group are gone. Probe
        the recorded process group directly: while any member survives — a
        shutdown/interrupt whose children outlived the leader, or a launcher
        that exited leaving compute children running — keep the record so the
        next run's crash recovery reaps the orphan before starting a new
        calculation over the same output.
        """
        recorded_pgid = process_record.get("pgid")
        pgid = recorded_pgid if isinstance(recorded_pgid, int) else proc.pid
        # Once poll() has reaped the Popen child, a live process at the same
        # numeric PID proves reuse and must never be treated as our old group.
        reaped = proc.poll() is not None
        reused = reaped and _reaped_pid_was_reused(proc.pid)
        if not reused and process_group_is_alive(pgid):
            return
        recorded_ticks = process_record.get("process_start_ticks")
        recorded_boot_id = process_record.get("process_boot_id")
        recorded_id = process_record.get("record_id")
        clear_orca_process_record(
            reaction_dir,
            pid=proc.pid,
            process_start_ticks=recorded_ticks if isinstance(recorded_ticks, int) else None,
            process_boot_id=(recorded_boot_id if isinstance(recorded_boot_id, str) else None),
            record_id=recorded_id if isinstance(recorded_id, str) else None,
        )
