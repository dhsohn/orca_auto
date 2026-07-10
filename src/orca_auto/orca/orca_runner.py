from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, SimpleNamespace
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
    """Raised when a supervisor SIGTERM stops the current ORCA run."""


class ShutdownSignalGuard:
    """Turn the first supervisor SIGTERM into ``WorkerShutdownInterrupt``, once.

    Terminating the ORCA process tree is itself interruptible: it sleeps while
    waiting for the group to die. Leaving the handler armed there let a second
    SIGTERM -- a cancel racing a worker shutdown, or systemd escalating -- unwind
    the cleanup it was running inside, leaving the ORCA leader reaped while its
    process group survived. The run then failed with a runner exception instead
    of being recorded as cancelled, and its admission slot refused to release.

    So the guard is armed only while waiting on ORCA. Callers disarm it before
    they start cleaning up; later signals are recorded in ``signalled`` and
    otherwise ignored.
    """

    def __init__(self) -> None:
        self._armed = False
        self._installed = False
        self._previous_handler: Any = None
        self.signalled = False

    def __enter__(self) -> ShutdownSignalGuard:
        try:
            self._previous_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_sigterm)
        except ValueError:
            # signal handlers can only be installed in the main thread
            return self
        self._installed = True
        self._armed = True
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._armed = False
        if not self._installed:
            return
        try:
            signal.signal(signal.SIGTERM, self._previous_handler)
        except ValueError:
            logger.debug("failed to restore SIGTERM handler outside main thread")

    def _handle_sigterm(self, _signum: int, _frame: FrameType | None) -> None:
        self.signalled = True
        if not self._armed:
            logger.warning("Ignoring SIGTERM received while cleaning up the ORCA process tree")
            return
        # Disarm before raising: the signal that interrupts the wait must not be
        # able to interrupt the unwinding it starts.
        self._armed = False
        raise WorkerShutdownInterrupt

    def disarm(self) -> None:
        self._armed = False

    @property
    def installed(self) -> bool:
        return self._installed


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
            if self._prepare_running_job is not None:
                self._prepare_running_job()
            proc: subprocess.Popen | None = None
            admission_registered = False
            try:
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
                process_record = write_orca_process_record(inp_path=inp, out_path=out, pid=proc.pid)
            except BaseException as exc:
                if proc is None:
                    if isinstance(exc, Exception) and self._register_running_job is not None:
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
            with ShutdownSignalGuard() as shutdown_guard:
                try:
                    if self._shutdown_requested is None:
                        return_code = proc.wait()
                    else:
                        while True:
                            if self._shutdown_requested():
                                raise WorkerShutdownInterrupt
                            try:
                                return_code = proc.wait(timeout=0.2)
                                break
                            except subprocess.TimeoutExpired:
                                continue
                    if not _reaped_pid_was_reused(proc.pid) and process_group_is_alive(
                        proc.pid,
                        killpg_fn=os.killpg,
                    ):
                        shutdown_guard.disarm()
                        logger.warning(
                            "ORCA launcher exited while its process group remained active; "
                            "retaining ownership until the group is gone"
                        )
                        self._retain_until_subprocess_tree_exits(proc)
                except WorkerShutdownInterrupt:
                    shutdown_guard.disarm()
                    handle.write(
                        "\n[orca_auto] interrupted by worker shutdown; "
                        "terminating ORCA process tree\n"
                    )
                    handle.flush()
                    self._retain_until_subprocess_tree_exits(proc)
                    raise
                except KeyboardInterrupt:
                    shutdown_guard.disarm()
                    handle.write(
                        "\n[orca_auto] interrupted by user; terminating ORCA process tree\n"
                    )
                    handle.flush()
                    self._retain_until_subprocess_tree_exits(proc)
                    raise
                finally:
                    # Releasing the process record and the admission slot must not
                    # be interruptible either: a SIGTERM landing here would strand
                    # the slot while its engine process is already gone.
                    shutdown_guard.disarm()
                    self._clear_process_record_if_group_gone(inp.parent, proc, process_record)
                    if admission_registered and self._register_running_job is not None:
                        self._register_running_job(None)
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
