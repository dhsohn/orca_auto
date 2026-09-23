from __future__ import annotations

import subprocess
from argparse import Namespace
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

import pytest

from orca_auto import cli_systemd_restart, cli_systemd_status, cli_systemd_units


def _restart_deps(**kwargs: Any) -> cli_systemd_restart.ServiceRestartDeps:
    """Keep legacy unit-selection tests independent of live admission evidence."""
    return cli_systemd_restart.ServiceRestartDeps(
        restart_guard=lambda *_args, **_kwargs: nullcontext(), **kwargs
    )


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
        deps=_restart_deps(
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
        deps=_restart_deps(
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
        deps=_restart_deps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert cli_systemd_status._selected_service_mode(statuses) == "worker-only"
    assert result == 0
    # Unit selection reads enablement only.
    assert not any(
        command[1] == "is-active" and command[2].endswith(".target") for command in commands
    )
    assert commands[-3:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "restart", "orca_auto-queue-worker@alice.service"),
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
        deps=_restart_deps(
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
        deps=_restart_deps(
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
        deps=_restart_deps(
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
    assert commands == [
        ("sudo", "-v"),
        ("sudo", "-n", "systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("sudo", "-n", "systemctl", "restart", "orca_auto-runtime@alice.target"),
        ("sudo", "-n", "systemctl", "restart", "orca_auto-queue-worker@alice.service"),
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
        deps=_restart_deps(
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
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
    ]


def test_cmd_service_restart_reloads_the_worker_the_target_leaves_running() -> None:
    """A target restart does not reload its member services; the workers must.

    Restarting only `orca_auto-runtime@<user>.target` left the worker's
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
        deps=_restart_deps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert ("systemctl", "restart", "orca_auto-queue-worker@alice.service") in commands


def test_cmd_service_restart_requires_sudo_for_non_root_user(capsys: Any) -> None:
    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=_restart_deps(
            is_root=lambda: False,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert "sudo is required to restart system services" in capsys.readouterr().err


def test_cmd_service_restart_default_guard_covers_every_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    guarded = False
    seen_units: list[tuple[str, ...]] = []

    @contextmanager
    def guard(worker_units: tuple[str, ...], **_kwargs: Any) -> Iterator[None]:
        nonlocal guarded
        seen_units.append(worker_units)
        guarded = True
        try:
            yield
        finally:
            guarded = False

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(argv))
        if argv[1] == "is-active":
            return subprocess.CompletedProcess(argv, 3, stdout="inactive\n", stderr="")
        assert guarded, f"unguarded state mutation: {argv}"
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_systemd_restart, "guard_service_restart", guard)
    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
        ),
    )
    assert result == 0
    assert seen_units == [("orca_auto-queue-worker@alice.service",)]
    assert not guarded
    assert [command[1] for command in commands] == [
        "reset-failed",
        "restart",
        "restart",
    ]


def test_cmd_service_restart_refusal_does_not_reset_failed_or_restart(capsys: Any) -> None:
    commands: list[tuple[str, ...]] = []

    @contextmanager
    def refuse(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        raise ValueError("active calculation remains")
        yield  # pragma: no cover

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_recording_run(commands, {"is-active": (3, "inactive\n")}),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
            restart_guard=refuse,
        ),
    )
    assert result == 1
    assert commands == []
    assert "active calculation remains" in capsys.readouterr().err


def test_cmd_service_restart_force_bypasses_guard_and_warns(capsys: Any) -> None:
    commands: list[tuple[str, ...]] = []

    def forbidden_guard(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("--force must not hold the admission lock during active-child cleanup")

    result = cli_systemd_restart.cmd_service_restart(
        Namespace(target_user="alice", force=True),
        deps=cli_systemd_restart.ServiceRestartDeps(
            is_root=lambda: True,
            run=_recording_run(commands, {"is-active": (3, "inactive\n")}),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
            restart_guard=forbidden_guard,
        ),
    )
    assert result == 0
    captured = capsys.readouterr()
    warning = (captured.out + captured.err).lower()
    assert "force" in warning and ("interrupt" in warning or "stop" in warning)
    assert any(command[1] == "restart" for command in commands)
