from __future__ import annotations

import subprocess
from argparse import Namespace
from collections.abc import Callable
from typing import Any

import pytest

from orca_auto import cli_systemd_restart, cli_systemd_status, cli_systemd_units


def _recording_run(
    commands: list[tuple[str, ...]],
    responses: dict[str, tuple[int, str] | tuple[int, str, str]],
    default: tuple[int, str] = (0, ""),
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Fake ``run`` that records every argv and answers by the ``argv[1]`` verb.

    ``responses`` maps a systemctl verb to ``(returncode, stdout)`` or
    ``(returncode, stdout, stderr)``; unmatched verbs answer ``default``.
    """

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        commands.append(tuple(argv))
        returncode, out, *rest = responses.get(argv[1], default)
        return subprocess.CompletedProcess(
            argv, returncode, stdout=out, stderr=rest[0] if rest else ""
        )

    return _fake_run


def test_cmd_service_restart_prefers_runtime_when_enabled(capsys: Any) -> None:
    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {
            "is-active": (3, "inactive\n"),
            "is-enabled": (0, "enabled\n"),
        },
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_restart.ServiceRestartDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert commands[-3:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-runtime@alice.target"),
        ("systemctl", "restart", "orca_auto-queue-worker@alice.service"),
    ]
    assert "Restarting orca_auto-runtime@alice.target" in capsys.readouterr().out


def test_cmd_service_restart_falls_back_to_engine_target_when_runtime_is_disabled() -> None:
    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {
            "is-active": (3, "inactive\n"),
            "is-enabled": (1, "disabled\n"),
        },
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_restart.ServiceRestartDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert commands[-3:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "restart", "orca_auto-queue-worker@alice.service"),
    ]


def test_cmd_service_restart_prefers_enabled_engine_target_over_active_runtime() -> None:
    commands: list[tuple[str, ...]] = []
    statuses = (
        cli_systemd_units.ServiceUnitStatus(
            "runtime", "orca_auto-runtime@alice.target", "active", "disabled"
        ),
        cli_systemd_units.ServiceUnitStatus(
            "engines", "orca_auto-engine-workers@alice.target", "active", "enabled"
        ),
        cli_systemd_units.ServiceUnitStatus(
            "worker", "orca_auto-queue-worker@alice.service", "active", "disabled"
        ),
    )

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        commands.append(tuple(argv))
        if argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout="loaded\n", stderr="")
        if argv[1] == "is-enabled":
            state = "enabled" if argv[2] == "orca_auto-engine-workers@alice.target" else "disabled"
            return subprocess.CompletedProcess(argv, 0, stdout=f"{state}\n", stderr="")
        if argv[1] == "is-active":
            return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert cli_systemd_status._selected_service_mode(statuses) == "worker-only"
    assert result == 0
    # Unit selection reads enablement only; is-active is reserved for deciding
    # whether the opt-in workflow worker is running.
    assert not any(
        command[1] == "is-active" and command[2].endswith(".target") for command in commands
    )
    assert commands[-5:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "reset-failed", "orca_auto-workflow-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "restart", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-workflow-worker@alice.service"),
    ]


def test_cmd_service_restart_uses_active_runtime_only_when_enablement_is_unreadable() -> None:
    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {
            "show": (0, "loaded\n"),
            "is-enabled": (1, "", "query failed\n"),
            "is-active": (0, "active\n"),
        },
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert commands[-5:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "reset-failed", "orca_auto-workflow-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-runtime@alice.target"),
        ("systemctl", "restart", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-workflow-worker@alice.service"),
    ]


def test_cmd_service_restart_directs_missing_install_to_installer(
    capsys: Any,
) -> None:
    commands: list[tuple[str, ...]] = []

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        commands.append(tuple(argv))
        if argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout="not-found\n", stderr="")
        pytest.fail(f"missing-unit detection must stop before state mutation: {argv}")

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert all(command[1] == "show" for command in commands)
    error = capsys.readouterr().err
    assert "required systemd units are not installed" in error
    assert "orca_auto systemd install --user alice --repo <repo>" in error


def test_cmd_service_restart_uses_sudo_for_non_root_user() -> None:
    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {"is-active": (3, "inactive\n")},
        default=(0, "inactive\n"),
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_restart.ServiceRestartDeps(
            default_service_user=lambda: "alice",
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
            is_root=lambda: False,
            run=_fake_run,
            which=lambda name: f"/usr/bin/{name}" if name in {"systemctl", "sudo"} else None,
        ),
    )

    assert result == 0
    # The is-active probe is a plain query, so it carries no sudo prefix.
    assert commands == [
        ("systemctl", "is-active", "orca_auto-workflow-worker@alice.service"),
        ("sudo", "systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("sudo", "systemctl", "restart", "orca_auto-runtime@alice.target"),
        ("sudo", "systemctl", "restart", "orca_auto-queue-worker@alice.service"),
    ]


def test_cmd_service_restart_stops_when_reset_failed_cannot_clear_start_limit() -> None:
    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {"is-active": (3, "inactive\n")},
        default=(5, "inactive\n"),
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_restart.ServiceRestartDeps(
            default_service_user=lambda: "alice",
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 5
    assert commands == [
        ("systemctl", "is-active", "orca_auto-workflow-worker@alice.service"),
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
    ]


def test_cmd_service_restart_reloads_the_worker_the_target_leaves_running() -> None:
    """A target restart does not reload its member services; the workers must.

    Restarting only `orca_auto-runtime@<user>.target` left both workers'
    ExecMainStartTimestamp untouched on the deploy host, so a worker kept
    serving pre-deploy code while the command reported success.
    """

    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {
            "show": (0, "loaded\n"),
            "is-enabled": (0, "enabled\n"),
            "is-active": (3, "inactive\n"),
        },
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert ("systemctl", "restart", "orca_auto-queue-worker@alice.service") in commands


def test_cmd_service_restart_restarts_a_running_workflow_worker() -> None:
    """The opt-in workflow worker belongs to no target, so only this reaches it.

    `service status` reports it and tells the operator to run `service restart`
    when it is stale, which is the unit that served stale code in the 8/3
    submission failures.
    """

    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {
            "show": (0, "loaded\n"),
            "is-enabled": (0, "enabled\n"),
            "is-active": (0, "active\n"),
        },
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert ("systemctl", "restart", "orca_auto-workflow-worker@alice.service") in commands


def _service_restart_with_workflow_state(
    workflow_state: str,
    *,
    workflow_returncode: int | None = None,
    workflow_stderr: str = "",
) -> tuple[int, list[tuple[str, ...]]]:
    commands: list[tuple[str, ...]] = []
    if workflow_returncode is None:
        workflow_returncode = 0 if workflow_state in {"active", "activating", "reloading"} else 3

    _fake_run = _recording_run(
        commands,
        {
            "show": (0, "loaded\n"),
            "is-enabled": (0, "enabled\n"),
            "is-active": (
                workflow_returncode,
                f"{workflow_state}\n" if workflow_state else "",
                workflow_stderr,
            ),
        },
    )

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )
    return result, commands


@pytest.mark.parametrize("workflow_state", ["failed", "activating", "reloading"])
def test_cmd_service_restart_recovers_a_broken_workflow_worker(workflow_state: str) -> None:
    """A crash loop is exactly where a bad deploy leaves the opt-in worker.

    `Restart=on-failure` with a start limit parks it in activating, then
    failed. Skipping those states would report success while the unit the
    operator was told to restart keeps running pre-deploy code -- and its
    tripped start limit would never be cleared.
    """

    result, commands = _service_restart_with_workflow_state(workflow_state)

    assert result == 0
    assert ("systemctl", "reset-failed", "orca_auto-workflow-worker@alice.service") in commands
    assert ("systemctl", "restart", "orca_auto-workflow-worker@alice.service") in commands


@pytest.mark.parametrize(
    ("workflow_state", "workflow_returncode"),
    [("inactive", 3), ("deactivating", 0), ("unknown", 3), ("", 3)],
)
def test_cmd_service_restart_leaves_a_stopped_workflow_worker_alone(
    workflow_state: str,
    workflow_returncode: int,
) -> None:
    """Supervision is opt-in, and a stop in flight is the operator's decision."""

    result, commands = _service_restart_with_workflow_state(
        workflow_state,
        workflow_returncode=workflow_returncode,
    )

    assert result == 0
    assert not any(
        "orca_auto-workflow-worker@alice.service" in command
        and command[1] in {"restart", "start", "reset-failed"}
        for command in commands
    )


def test_cmd_service_restart_stops_when_the_workflow_state_is_unreadable(capsys: Any) -> None:
    """An unreadable state must not be reported as a clean restart."""

    result, commands = _service_restart_with_workflow_state(
        "Failed to connect to bus: No such file or directory",
        workflow_returncode=1,
    )

    assert result == 1
    assert not any(command[1] in {"restart", "reset-failed"} for command in commands), (
        "nothing may be mutated once the workflow worker's state is unknown"
    )
    assert "cannot tell whether" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("workflow_returncode", "workflow_state", "workflow_stderr"),
    [
        (4, "", ""),
        (4, "unknown", ""),
        (1, "active", "Failed to connect to bus: No such file or directory"),
    ],
)
def test_cmd_service_restart_fails_closed_for_inconsistent_workflow_query(
    capsys: Any,
    workflow_returncode: int,
    workflow_state: str,
    workflow_stderr: str,
) -> None:
    result, commands = _service_restart_with_workflow_state(
        workflow_state,
        workflow_returncode=workflow_returncode,
        workflow_stderr=workflow_stderr,
    )

    assert result == 1
    assert not any(command[1] in {"restart", "reset-failed"} for command in commands)
    assert "cannot tell whether" in capsys.readouterr().err


def test_cmd_service_restart_requires_sudo_for_non_root_user(capsys: Any) -> None:
    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: False,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert "sudo is required to restart system services" in capsys.readouterr().err
