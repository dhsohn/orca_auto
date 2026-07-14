from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36
_GRACEFUL_TIMEOUT_SECONDS = 5.0
_KILL_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05


class _ProcessTreeInspectionError(RuntimeError):
    pass


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(option, value, 0, 0, 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _configure_linux_supervisor(parent_pid: int) -> None:
    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    if os.getppid() != parent_pid:
        raise RuntimeError("smoke runner exited before process supervision was established")


def _process_start_ticks(pid: int) -> int | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _ProcessTreeInspectionError(
            f"could not inspect process identity for pid {pid}"
        ) from exc
    command_end = raw.rfind(")")
    if command_end < 0:
        raise _ProcessTreeInspectionError(f"invalid /proc stat record for pid {pid}")
    fields = raw[command_end + 2 :].split()
    if len(fields) <= 19:
        raise _ProcessTreeInspectionError(f"incomplete /proc stat record for pid {pid}")
    try:
        return int(fields[19])
    except ValueError as exc:
        raise _ProcessTreeInspectionError(f"invalid process start time for pid {pid}") from exc


def _direct_children(pid: int) -> tuple[int, ...]:
    children_path = Path("/proc") / str(pid) / "task" / str(pid) / "children"
    try:
        raw = children_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise _ProcessTreeInspectionError(f"could not inspect children for pid {pid}") from exc
    try:
        return tuple(int(value) for value in raw.split())
    except ValueError as exc:
        raise _ProcessTreeInspectionError(f"invalid child process list for pid {pid}") from exc


def _descendant_identities(supervisor_pid: int) -> dict[int, int]:
    descendants: dict[int, int] = {}
    pending = [supervisor_pid]
    inspected = {supervisor_pid}
    while pending:
        parent_pid = pending.pop()
        for child_pid in _direct_children(parent_pid):
            if child_pid in inspected:
                continue
            inspected.add(child_pid)
            start_ticks = _process_start_ticks(child_pid)
            if start_ticks is None:
                continue
            descendants[child_pid] = start_ticks
            pending.append(child_pid)
    return descendants


def _signal_identities(identities: dict[int, int], signum: int) -> None:
    for pid, expected_start_ticks in identities.items():
        try:
            if _process_start_ticks(pid) != expected_start_ticks:
                continue
            os.kill(pid, signum)
        except (ProcessLookupError, PermissionError):
            continue


def _reap_adopted_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid <= 0:
            return


def _poll_primary(process: subprocess.Popen[bytes]) -> int | None:
    return process.poll()


def _wait_for_tree_exit(
    process: subprocess.Popen[bytes],
    *,
    signum: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    supervisor_pid = os.getpid()
    while True:
        try:
            identities = _descendant_identities(supervisor_pid)
            _signal_identities(identities, signum)
            primary_exited = _poll_primary(process) is not None
            if primary_exited:
                _reap_adopted_children()
                identities = _descendant_identities(supervisor_pid)
                if not identities:
                    return True
        except (OSError, RuntimeError, ValueError):
            # Process inspection and reaping are part of the proof of exit. A
            # transient failure therefore consumes the bounded phase and is
            # retried; it must never be interpreted as an empty tree.
            identities = {-1: -1}

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _terminate_and_reap_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        if _wait_for_tree_exit(
            process,
            signum=signal.SIGTERM,
            timeout_seconds=_GRACEFUL_TIMEOUT_SECONDS,
        ):
            return
        if _wait_for_tree_exit(
            process,
            signum=signal.SIGKILL,
            timeout_seconds=_KILL_TIMEOUT_SECONDS,
        ):
            return
    except Exception:  # noqa: BLE001 - retain ownership after any cleanup uncertainty
        pass

    # An owner that cannot prove its detached descendants are gone must remain
    # alive. This keeps production admission fail-closed until the process tree
    # can be killed and reaped or exits independently.
    while True:
        try:
            if _wait_for_tree_exit(
                process,
                signum=signal.SIGKILL,
                timeout_seconds=_POLL_INTERVAL_SECONDS,
            ):
                return
        except Exception:  # noqa: BLE001 - remain alive and retry fail-closed
            pass
        time.sleep(_POLL_INTERVAL_SECONDS)


def _await_launch_gate(gate_fd: int, shutdown_requested: Callable[[], bool]) -> bool:
    try:
        while not shutdown_requested():
            readable, _writable, _exceptional = select.select(
                [gate_fd],
                [],
                [],
                _POLL_INTERVAL_SECONDS,
            )
            if not readable:
                continue
            return os.read(gate_fd, 1) == b"1"
        return False
    finally:
        os.close(gate_fd)


def _publish_cleanup_proof(proof_fd: int) -> None:
    if os.write(proof_fd, b"1") != 1:
        raise OSError("could not publish smoke process-tree cleanup proof")


def run_supervisor(gate_fd: int, proof_fd: int, command: Sequence[str]) -> int:
    if not command:
        raise ValueError("supervised smoke command must not be empty")
    parent_pid = os.getppid()
    shutdown_signal = 0

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal shutdown_signal
        if shutdown_signal == 0:
            shutdown_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, request_shutdown)

    _configure_linux_supervisor(parent_pid)
    if not _await_launch_gate(gate_fd, lambda: shutdown_signal != 0):
        _publish_cleanup_proof(proof_fd)
        return 128 + shutdown_signal if shutdown_signal else 125

    process = subprocess.Popen(command)
    try:
        while shutdown_signal == 0:
            try:
                return_code = process.wait(timeout=_POLL_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                continue
            _terminate_and_reap_tree(process)
            _publish_cleanup_proof(proof_fd)
            return return_code

        _terminate_and_reap_tree(process)
        _publish_cleanup_proof(proof_fd)
        return 128 + shutdown_signal
    except BaseException:
        _terminate_and_reap_tree(process)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3:
        print(
            "usage: _process_supervisor GATE_FD PROOF_FD COMMAND [ARG ...]",
            file=sys.stderr,
        )
        return 2
    try:
        gate_fd = int(arguments[0])
        proof_fd = int(arguments[1])
    except ValueError:
        print("smoke supervisor file descriptors must be integers", file=sys.stderr)
        return 2
    try:
        return run_supervisor(gate_fd, proof_fd, arguments[2:])
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"smoke process supervision failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 125
    finally:
        try:
            os.close(proof_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
