from __future__ import annotations

import logging
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from orca_auto.core.queue.processes import ProcessGroupTerminationDeps, terminate_process_group

from .orca_process import (
    clear_orca_process_record,
    process_group_is_alive,
    write_orca_process_record,
)

logger = logging.getLogger(__name__)


class WorkerShutdownInterrupt(KeyboardInterrupt):
    """Raised when a supervisor SIGTERM stops the current ORCA run."""


@dataclass
class RunResult:
    out_path: str
    return_code: int


class OrcaRunner:
    def __init__(self, orca_executable: str) -> None:
        self.orca_executable = orca_executable

    def _terminate_subprocess_tree(self, proc: subprocess.Popen) -> bool:
        """Terminate the ORCA process group; True only when it is confirmed gone."""
        if proc.poll() is not None:
            return True
        logger.warning("Terminating ORCA process tree (pid=%d)", proc.pid)
        return terminate_process_group(
            proc,
            graceful_timeout=3,
            kill_timeout=5,
            killpg_fn=os.killpg,
            sigterm=signal.SIGTERM,
            sigkill=signal.SIGKILL,
            deps=ProcessGroupTerminationDeps(logger=logger),
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
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                process_record = write_orca_process_record(inp_path=inp, out_path=out, pid=proc.pid)
            except Exception:
                self._terminate_subprocess_tree(proc)
                raise
            prev_sigterm_handler = None
            sigterm_handler_installed = False

            def _sigterm_to_worker_shutdown(_signum: int, _frame: FrameType | None) -> None:
                raise WorkerShutdownInterrupt

            try:
                prev_sigterm_handler = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, _sigterm_to_worker_shutdown)
                sigterm_handler_installed = True
            except ValueError:
                # signal handlers can only be installed in the main thread
                sigterm_handler_installed = False
            try:
                return_code = proc.wait()
            except WorkerShutdownInterrupt:
                handle.write(
                    "\n[orca_auto] interrupted by worker shutdown; terminating ORCA process tree\n"
                )
                handle.flush()
                self._terminate_subprocess_tree(proc)
                raise
            except KeyboardInterrupt:
                handle.write("\n[orca_auto] interrupted by user; terminating ORCA process tree\n")
                handle.flush()
                self._terminate_subprocess_tree(proc)
                raise
            finally:
                if sigterm_handler_installed:
                    try:
                        signal.signal(signal.SIGTERM, prev_sigterm_handler)
                    except ValueError:
                        logger.debug("failed to restore SIGTERM handler outside main thread")
                self._clear_process_record_if_group_gone(inp.parent, proc, process_record)
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
        if process_group_is_alive(pgid):
            return
        recorded_ticks = process_record.get("process_start_ticks")
        clear_orca_process_record(
            reaction_dir,
            pid=proc.pid,
            process_start_ticks=recorded_ticks if isinstance(recorded_ticks, int) else None,
        )
