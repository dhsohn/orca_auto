from __future__ import annotations

import argparse
import errno
import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.cli_errors import emit_error
from orca_auto.systemd_plan import (
    DEFAULT_SYSTEMD_UNIT_DIR,
    SystemdInstallPlan,
    _format_command,
    _is_root,
    _print_plan,
    _print_warnings,
    _systemd_command_argv,
    _validate_target_user,
    build_systemd_install_plan,
    managed_runtime_units_for_user,
)

SYSTEMD_TRANSITION_LOCK_TIMEOUT_SECONDS = 60.0
_SYSTEMD_TRANSITION_LOCK_RETRY_SECONDS = 0.01
_SYSTEMD_TRANSITION_SOCKET_PROTOCOL = b"orca_auto.systemd-transition-lock.v1"
_SYSTEMD_TRANSITION_SOCKET_PREFIX = b"\0orca_auto-sd-transition-v1-"


@dataclass(frozen=True)
class _HeldSystemdTransitionLock:
    pid: int
    socket: socket.socket


class _SystemdTransitionThreadState(threading.local):
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.held: dict[bytes, _HeldSystemdTransitionLock] = {}


_SYSTEMD_TRANSITION_REGISTRY_GUARD = threading.RLock()
_SYSTEMD_TRANSITION_REGISTERED_SOCKETS: dict[int, socket.socket] = {}
_SYSTEMD_TRANSITION_THREAD_STATE = _SystemdTransitionThreadState()


def _current_thread_systemd_transition_locks() -> dict[bytes, _HeldSystemdTransitionLock]:
    pid = os.getpid()
    if _SYSTEMD_TRANSITION_THREAD_STATE.pid != pid:
        _SYSTEMD_TRANSITION_THREAD_STATE.pid = pid
        _SYSTEMD_TRANSITION_THREAD_STATE.held = {}
    return _SYSTEMD_TRANSITION_THREAD_STATE.held


def _systemd_transition_socket_address(target_user: str) -> bytes:
    """Return one bounded identity per user within one Linux network namespace."""

    safe_user = _validate_target_user(target_user.strip())
    digest = hashlib.sha256(
        _SYSTEMD_TRANSITION_SOCKET_PROTOCOL + b"\0" + safe_user.encode("ascii")
    ).hexdigest()
    address = _SYSTEMD_TRANSITION_SOCKET_PREFIX + digest.encode("ascii")
    if len(address) > 107:
        raise RuntimeError("systemd transition socket address exceeds Linux AF_UNIX limit")
    return address


def _before_systemd_transition_fork() -> None:
    _SYSTEMD_TRANSITION_REGISTRY_GUARD.acquire()


def _after_systemd_transition_fork_in_parent() -> None:
    _SYSTEMD_TRANSITION_REGISTRY_GUARD.release()


def _after_systemd_transition_fork_in_child() -> None:
    global _SYSTEMD_TRANSITION_THREAD_STATE

    try:
        for held_socket in tuple(_SYSTEMD_TRANSITION_REGISTERED_SOCKETS.values()):
            with suppress(OSError):
                held_socket.close()
        _SYSTEMD_TRANSITION_REGISTERED_SOCKETS.clear()
        _SYSTEMD_TRANSITION_THREAD_STATE = _SystemdTransitionThreadState()
    finally:
        _SYSTEMD_TRANSITION_REGISTRY_GUARD.release()


os.register_at_fork(
    before=_before_systemd_transition_fork,
    after_in_parent=_after_systemd_transition_fork_in_parent,
    after_in_child=_after_systemd_transition_fork_in_child,
)


def _bind_systemd_transition_socket(address: bytes, *, deadline: float) -> socket.socket:
    while True:
        bind_error: OSError | None = None
        with _SYSTEMD_TRANSITION_REGISTRY_GUARD:
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            candidate.set_inheritable(False)
            try:
                candidate.bind(address)
            except OSError as exc:
                bind_error = exc
                candidate.close()
            else:
                _SYSTEMD_TRANSITION_REGISTERED_SOCKETS[candidate.fileno()] = candidate
                return candidate

        if bind_error is None or bind_error.errno != errno.EADDRINUSE:
            if bind_error is None:
                raise RuntimeError("systemd transition socket bind failed without an error")
            raise bind_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "timed out waiting for the systemd transition lock in this Linux network namespace"
            )
        time.sleep(min(_SYSTEMD_TRANSITION_LOCK_RETRY_SECONDS, remaining))


def _release_systemd_transition_socket(held: _HeldSystemdTransitionLock) -> None:
    with _SYSTEMD_TRANSITION_REGISTRY_GUARD:
        descriptor = held.socket.fileno()
        if _SYSTEMD_TRANSITION_REGISTERED_SOCKETS.get(descriptor) is held.socket:
            _SYSTEMD_TRANSITION_REGISTERED_SOCKETS.pop(descriptor, None)
        held.socket.close()


@contextmanager
def systemd_transition_lock(target_user: str) -> Iterator[None]:
    """Serialize one user's callers within the same Linux network namespace."""

    address = _systemd_transition_socket_address(target_user)
    thread_locks = _current_thread_systemd_transition_locks()
    existing = thread_locks.get(address)
    if existing is not None:
        with _SYSTEMD_TRANSITION_REGISTRY_GUARD:
            descriptor = existing.socket.fileno()
            registered = (
                existing.pid == os.getpid()
                and descriptor >= 0
                and _SYSTEMD_TRANSITION_REGISTERED_SOCKETS.get(descriptor) is existing.socket
            )
        if not registered:
            raise ValueError("systemd transition lock is no longer held safely")
        yield
        return

    deadline = time.monotonic() + SYSTEMD_TRANSITION_LOCK_TIMEOUT_SECONDS
    held_socket = _bind_systemd_transition_socket(address, deadline=deadline)
    held = _HeldSystemdTransitionLock(pid=os.getpid(), socket=held_socket)
    thread_locks[address] = held
    try:
        yield
    finally:
        if thread_locks.get(address) is held:
            thread_locks.pop(address, None)
        _release_systemd_transition_socket(held)


def _emit_transition_lock_error(target_user: str, exc: BaseException) -> None:
    emit_error(f"could not acquire systemd transition lock for {target_user}: {exc}")


def _run_command(
    command: Sequence[str],
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    argv = _systemd_command_argv(command, use_sudo=use_sudo)
    print(f"$ {_format_command(command, use_sudo=use_sudo)}")
    try:
        completed = run(argv, check=False)
    except OSError as exc:
        emit_error(
            f"command failed to execute: {_format_command(command, use_sudo=use_sudo)}: {exc}"
        )
        return 1
    return int(completed.returncode)


def _query_unit_active_state(
    unit: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[int, str] | None:
    command = ("systemctl", "is-active", unit)
    argv = _systemd_command_argv(command, use_sudo=use_sudo)
    print(f"$ {_format_command(command, use_sudo=use_sudo)}")
    try:
        completed = run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        emit_error(f"could not query systemd unit {unit}: {exc}")
        return None
    stdout = str(completed.stdout or "").strip()
    stderr = str(completed.stderr or "").strip()
    state = (stdout or stderr).splitlines()[0] if stdout or stderr else ""
    return int(completed.returncode), state


def _known_unit_activity(result: tuple[int, str]) -> str | None:
    if result == (0, "active"):
        return "active"
    if result in {
        (3, "inactive"),
        (3, "failed"),
        (3, "not-found"),
        (4, "unknown"),
        (4, "not-found"),
    }:
        return result[1]
    return None


def _require_unit_state(
    unit: str,
    expected: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    result = _query_unit_active_state(unit, use_sudo=use_sudo, run=run)
    if result is None:
        return False
    rc, state = result
    activity = _known_unit_activity(result)
    if (expected == "active" and activity == "active") or (
        expected == "inactive" and activity in {"inactive", "failed", "unknown", "not-found"}
    ):
        return True
    emit_error(
        f"systemd unit {unit} must be {expected} before continuing; "
        f"observed {state or 'no state'} (exit {rc})"
    )
    return False


def _require_all_managed_units_inactive(
    target_user: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """Require an offline supervised graph without stopping any service."""

    all_inactive = True
    for unit in managed_runtime_units_for_user(target_user):
        if not _require_unit_state(unit, "inactive", use_sudo=use_sudo, run=run):
            all_inactive = False
    return all_inactive


@dataclass(frozen=True)
class _StoppedSystemdRuntime:
    target_user: str
    restore_workflow: bool


def _selected_runtime_is_managed(target_user: str, selected_unit: str) -> bool:
    runtime_unit, worker_unit, _, _ = managed_runtime_units_for_user(target_user)
    return selected_unit in {runtime_unit, worker_unit}


def _stop_managed_systemd_runtime(
    target_user: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, _StoppedSystemdRuntime | None]:
    """Snapshot, stop, and verify the complete supervised runtime graph."""

    managed_units = managed_runtime_units_for_user(target_user)
    _, worker_unit, _, workflow_unit = managed_units

    activity: dict[str, str] = {}
    for unit in managed_units:
        result = _query_unit_active_state(unit, use_sudo=use_sudo, run=run)
        if result is None:
            return 1, None
        state = _known_unit_activity(result)
        if state is None:
            rc, observed = result
            emit_error(
                f"cannot safely snapshot systemd unit {unit}: "
                f"observed {observed or 'no state'} (exit {rc})"
            )
            return rc or 1, None
        activity[unit] = state

    active_units = tuple(unit for unit in managed_units if activity[unit] == "active")
    if active_units:
        print("Stopping active supervised orca_auto runtimes before loading the selected build")
        rc = _run_command(
            ("systemctl", "stop", *active_units),
            use_sudo=use_sudo,
            run=run,
        )
        if rc != 0:
            return rc, None

    for unit in managed_units:
        if not _require_unit_state(unit, "inactive", use_sudo=use_sudo, run=run):
            return 1, None

    if activity[worker_unit] == "failed":
        rc = _run_command(
            ("systemctl", "reset-failed", worker_unit),
            use_sudo=use_sudo,
            run=run,
        )
        if rc != 0:
            return rc, None

    return 0, _StoppedSystemdRuntime(
        target_user=target_user,
        restore_workflow=activity[workflow_unit] == "active",
    )


def _start_managed_systemd_runtime(
    stopped: _StoppedSystemdRuntime,
    selected_unit: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Start and verify a selected runtime after the old graph is stopped."""

    target_user = stopped.target_user
    runtime_unit, worker_unit, bot_unit, workflow_unit = managed_runtime_units_for_user(target_user)
    if not _selected_runtime_is_managed(target_user, selected_unit):
        emit_error(f"refusing to start unmanaged systemd unit: {selected_unit}")
        return 1

    def fail_stopped(rc: int) -> int:
        emit_error("new runtime start/restore failed; stopping the supervised graph")
        stop_rc = _run_command(
            ("systemctl", "stop", *managed_runtime_units_for_user(target_user)),
            use_sudo=use_sudo,
            run=run,
        )
        inactive = _require_all_managed_units_inactive(
            target_user,
            use_sudo=use_sudo,
            run=run,
        )
        if stop_rc == 0 and inactive:
            emit_error("the supervised graph remains stopped")
        else:
            emit_error("could not verify a fully stopped graph after the start failure")
        return rc or stop_rc or 1

    print(f"Starting selected runtime {selected_unit}")
    rc = _run_command(
        ("systemctl", "start", selected_unit),
        use_sudo=use_sudo,
        run=run,
    )
    if rc != 0:
        return fail_stopped(rc)
    required_active_units = (
        (runtime_unit, worker_unit, bot_unit) if selected_unit == runtime_unit else (worker_unit,)
    )
    for unit in required_active_units:
        if not _require_unit_state(unit, "active", use_sudo=use_sudo, run=run):
            return fail_stopped(1)

    if stopped.restore_workflow:
        print(f"Restoring previously active workflow unit {workflow_unit}")
        rc = _run_command(
            ("systemctl", "start", workflow_unit),
            use_sudo=use_sudo,
            run=run,
        )
        if rc != 0:
            return fail_stopped(rc)
        if not _require_unit_state(workflow_unit, "active", use_sudo=use_sudo, run=run):
            return fail_stopped(1)
    else:
        print(f"Leaving previously inactive workflow unit stopped: {workflow_unit}")
    return 0


def _replace_selected_systemd_runtime_locked(
    target_user: str,
    selected_unit: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Replace all supervised runtimes without allowing a split-build graph."""

    if not _selected_runtime_is_managed(target_user, selected_unit):
        emit_error(f"refusing to start unmanaged systemd unit: {selected_unit}")
        return 1
    rc, stopped = _stop_managed_systemd_runtime(
        target_user,
        use_sudo=use_sudo,
        run=run,
    )
    if rc != 0 or stopped is None:
        return rc or 1
    return _start_managed_systemd_runtime(
        stopped,
        selected_unit,
        use_sudo=use_sudo,
        run=run,
    )


def replace_selected_systemd_runtime(
    target_user: str,
    selected_unit: str,
    *,
    use_sudo: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Replace one user's runtime while holding the canonical transition lock."""

    try:
        with systemd_transition_lock(target_user):
            return _replace_selected_systemd_runtime_locked(
                target_user,
                selected_unit,
                use_sudo=use_sudo,
                run=run,
            )
    except (OSError, ValueError) as exc:
        _emit_transition_lock_error(target_user, exc)
        return 1


def _write_units_direct(plan: SystemdInstallPlan) -> None:
    plan.unit_dir.mkdir(parents=True, exist_ok=True)
    for unit in plan.units:
        unit.destination.write_text(unit.content, encoding="utf-8")
        unit.destination.chmod(0o644)
        print(f"installed: {unit.destination}")


def _write_units_with_sudo(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    with tempfile.TemporaryDirectory(prefix="orca_auto-systemd-") as tmp_dir_text:
        tmp_dir = Path(tmp_dir_text)
        for unit in plan.units:
            (tmp_dir / unit.name).write_text(unit.content, encoding="utf-8")

        rc = _run_command(("mkdir", "-p", str(plan.unit_dir)), use_sudo=True, run=run)
        if rc != 0:
            return rc
        for unit in plan.units:
            rc = _run_command(
                ("install", "-m", "0644", str(tmp_dir / unit.name), str(unit.destination)),
                use_sudo=True,
                run=run,
            )
            if rc != 0:
                return rc
            print(f"installed: {unit.destination}")
    return 0


def _install_failure(
    rc: int,
    *,
    stopped: _StoppedSystemdRuntime | None,
) -> int:
    if stopped is not None:
        emit_error(
            "systemd update failed after the old runtime was drained; "
            "the supervised graph remains stopped"
        )
    return rc or 1


def _apply_systemd_install_plan_locked(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    python_path = plan.repo / ".venv" / "bin" / "python"
    if not python_path.is_file() or not os.access(python_path, os.X_OK):
        emit_error(f"service Python is missing or not executable: {python_path}; run `make venv`")
        return 1

    if plan.use_sudo and shutil.which("sudo") is None:
        emit_error("sudo is required to write system units; rerun as root or use --no-sudo")
        return 1

    stopped: _StoppedSystemdRuntime | None = None
    if plan.live_transition:
        if plan.enabled_unit is None or not _selected_runtime_is_managed(
            plan.target_user,
            plan.enabled_unit,
        ):
            emit_error("live systemd transition has no managed selected runtime unit")
            return 1
        rc, stopped = _stop_managed_systemd_runtime(
            plan.target_user,
            use_sudo=plan.use_sudo,
            run=run,
        )
        if rc != 0 or stopped is None:
            return rc or 1
    elif plan.requires_inactive_preflight:
        print("Verifying every supervised orca_auto unit is inactive before writing units")
        if not _require_all_managed_units_inactive(
            plan.target_user,
            use_sudo=plan.use_sudo,
            run=run,
        ):
            emit_error("refusing to write systemd units while the supervised graph is not offline")
            return 1

    if plan.use_sudo:
        rc = _write_units_with_sudo(plan, run=run)
    else:
        try:
            _write_units_direct(plan)
        except OSError as exc:
            emit_error(f"failed to write systemd units: {exc}")
            return _install_failure(1, stopped=stopped)
        rc = 0
    if rc != 0:
        return _install_failure(rc, stopped=stopped)

    for command in plan.commands:
        rc = _run_command(command, use_sudo=plan.use_sudo, run=run)
        if rc != 0:
            return _install_failure(rc, stopped=stopped)

    if plan.live_transition:
        if plan.enabled_unit is None or stopped is None:
            emit_error("live systemd transition lost its stopped-runtime snapshot")
            return _install_failure(1, stopped=stopped)
        rc = _start_managed_systemd_runtime(
            stopped,
            plan.enabled_unit,
            use_sudo=plan.use_sudo,
            run=run,
        )
        if rc != 0:
            return rc

    if plan.enabled_unit:
        print(f"enabled: {plan.enabled_unit}")
    else:
        print("installed systemd units; enable/start skipped")
    return 0


def apply_systemd_install_plan(
    plan: SystemdInstallPlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Apply one install plan while serializing all transitions for its user."""

    try:
        with systemd_transition_lock(plan.target_user):
            return _apply_systemd_install_plan_locked(plan, run=run)
    except (OSError, ValueError) as exc:
        _emit_transition_lock_error(plan.target_user, exc)
        return 1


@dataclass(frozen=True)
class SystemdInstallCliDeps:
    """Optional overrides for system-effect seams (test injection)."""

    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None
    is_root: Callable[[], bool] | None = None


def cmd_systemd_install(
    args: argparse.Namespace, *, deps: SystemdInstallCliDeps | None = None
) -> int:
    deps = deps or SystemdInstallCliDeps()
    run = deps.run or subprocess.run
    is_root = deps.is_root or _is_root

    try:
        plan = build_systemd_install_plan(
            target_user=getattr(args, "target_user", None),
            repo=getattr(args, "repo", None),
            config=getattr(args, "config", None),
            unit_dir=getattr(args, "unit_dir", DEFAULT_SYSTEMD_UNIT_DIR),
            worker_only=bool(getattr(args, "worker_only", False)),
            no_enable=bool(getattr(args, "no_enable", False)),
            no_start=bool(getattr(args, "no_start", False)),
            no_sudo=bool(getattr(args, "no_sudo", False)),
            is_root=is_root,
        )
    except (OSError, ValueError) as exc:
        emit_error(exc)
        return 1

    _print_warnings(plan)
    if bool(getattr(args, "dry_run", False)):
        _print_plan(plan)
        return 0
    return int(apply_systemd_install_plan(plan, run=run))


__all__ = [
    "SystemdInstallCliDeps",
    "apply_systemd_install_plan",
    "cmd_systemd_install",
    "replace_selected_systemd_runtime",
    "systemd_transition_lock",
]
