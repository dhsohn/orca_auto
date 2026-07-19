from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys
import threading
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli_systemd_apply, cli_systemd_status, systemd_plan


class _SystemdStateRunner:
    def __init__(
        self,
        *,
        runtime_active: bool = False,
        runtime_enabled: bool = True,
        workflow_active: bool = False,
        stop_rc: int = 0,
        stuck_active_unit: str | None = None,
        reset_rc: int = 0,
        disable_rc: int = 0,
        enable_rc: int = 0,
        query_error_unit: str | None = None,
        start_failures: dict[str, int] | None = None,
        start_inactive_units: set[str] | None = None,
    ) -> None:
        user = "alice"
        self.runtime = f"orca_auto-runtime@{user}.target"
        self.worker = f"orca_auto-queue-worker@{user}.service"
        self.bot = f"orca_auto-bot@{user}.service"
        self.workflow = f"orca_auto-workflow-worker@{user}.service"
        self.states = {
            self.runtime: "active" if runtime_active else "inactive",
            self.worker: "active" if runtime_active else "inactive",
            self.bot: "active" if runtime_active else "inactive",
            self.workflow: "active" if workflow_active else "inactive",
        }
        self.enabled = {
            self.runtime: "enabled" if runtime_enabled else "disabled",
            self.worker: "disabled" if runtime_enabled else "enabled",
        }
        self.stop_rc = stop_rc
        self.stuck_active_unit = stuck_active_unit
        self.reset_rc = reset_rc
        self.disable_rc = disable_rc
        self.enable_rc = enable_rc
        self.query_error_unit = query_error_unit
        self.start_failures = start_failures or {}
        self.start_inactive_units = start_inactive_units or set()
        self.commands: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: tuple[str, ...] | list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        command = tuple(argv)
        self.commands.append(command)
        systemctl = command[1:] if command[0] == "sudo" else command
        action = systemctl[1]
        units = systemctl[2:]

        if action == "is-active":
            if units[0] == self.query_error_unit:
                raise OSError("systemd query unavailable")
            state = self.states.get(units[0], "inactive")
            return_code = 0 if state == "active" else 4 if state in {"not-found", "unknown"} else 3
            return subprocess.CompletedProcess(
                argv,
                return_code,
                stdout=f"{state}\n",
                stderr="",
            )
        if action == "is-enabled":
            state = self.enabled.get(units[0], "disabled")
            return subprocess.CompletedProcess(
                argv,
                0 if state == "enabled" else 1,
                stdout=f"{state}\n",
                stderr="",
            )
        if action == "enable":
            if self.enable_rc:
                return subprocess.CompletedProcess(argv, self.enable_rc, stdout="", stderr="")
            self.enabled[units[0]] = "enabled"
        elif action == "disable":
            if self.disable_rc:
                return subprocess.CompletedProcess(argv, self.disable_rc, stdout="", stderr="")
            self.enabled[units[0]] = "disabled"
        elif action == "stop":
            if self.stop_rc:
                return subprocess.CompletedProcess(argv, self.stop_rc, stdout="", stderr="")
            for unit in units:
                self.states[unit] = "inactive"
            if self.stuck_active_unit is not None:
                self.states[self.stuck_active_unit] = "active"
        elif action == "reset-failed":
            return subprocess.CompletedProcess(argv, self.reset_rc, stdout="", stderr="")
        elif action == "start":
            unit = units[0]
            failure = self.start_failures.get(unit, 0)
            if failure:
                return subprocess.CompletedProcess(argv, failure, stdout="", stderr="")
            if unit not in self.start_inactive_units:
                self.states[unit] = "active"
            if unit == self.runtime:
                for child_unit in (self.worker, self.bot):
                    if child_unit not in self.start_inactive_units:
                        self.states[child_unit] = "active"
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "orca_auto"
    python_path = repo / ".venv" / "bin" / "python"
    config_path = repo / "config" / "orca_auto.yaml"
    python_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    runs_root = repo / "orca_runs"
    admission_root = repo / "admission"
    runs_root.mkdir()
    admission_root.mkdir()
    orca_executable = repo / "orca"
    orca_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    orca_executable.chmod(0o755)
    python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    python_path.chmod(0o755)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "scheduler:",
                f"  admission_root: {repo / 'admission'}",
                "messenger:",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '123'",
                "orca:",
                "  paths:",
                f"    orca_executable: {orca_executable}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return repo, config_path


def test_systemd_telegram_lookup_rejects_legacy_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "telegram:\n  bot_token: legacy-token\n  chat_id: legacy-chat\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="messenger.telegram"):
        systemd_plan._telegram_mapping(config_path)


def test_systemd_telegram_lookup_rejects_malformed_empty_adapter_section(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text("messenger:\n  telegram: ''\n", encoding="utf-8")

    with pytest.raises(ValueError, match="messenger.telegram section is not a mapping"):
        systemd_plan._telegram_mapping(config_path)


def test_build_systemd_install_plan_renders_repo_and_config_paths(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=unit_dir,
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-runtime@alice.target"
    assert plan.use_sudo is False
    assert plan.warnings == ()
    assert plan.commands == (
        ("systemctl", "daemon-reload"),
        ("systemctl", "disable", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "enable", "orca_auto-runtime@alice.target"),
    )
    assert plan.requires_inactive_preflight is False
    assert plan.live_transition is True

    unit_by_name = {unit.name: unit for unit in plan.units}
    worker_content = unit_by_name["orca_auto-queue-worker@.service"].content
    workflow_worker_content = unit_by_name["orca_auto-workflow-worker@.service"].content
    assert "queue worker --app orca" in worker_content
    assert "queue worker --app workflow" in workflow_worker_content
    assert f"WorkingDirectory={repo.resolve(strict=False)}" in worker_content
    assert f"Environment=ORCA_AUTO_CONFIG={config_path.resolve(strict=False)}" in worker_content
    assert f"ExecStart={repo.resolve(strict=False)}/.venv/bin/python" in worker_content
    assert "NoNewPrivileges=true" in worker_content
    assert "PrivateTmp=true" in worker_content
    assert "ProtectSystem=full" in worker_content
    assert "ProtectHome=read-only" in worker_content
    assert "UMask=0077" in worker_content
    assert "KillMode=control-group" in worker_content
    assert "TimeoutStopSec=30" in worker_content
    assert "StartLimitIntervalSec=300" in worker_content
    assert "StartLimitBurst=3" in worker_content
    assert "Restart=on-failure" in worker_content
    assert "RestartSec=30" in worker_content
    assert (
        "ReadWritePaths="
        f"{repo.resolve(strict=False) / 'admission'} "
        f"{repo.resolve(strict=False) / 'orca_runs'}"
    ) in worker_content
    bot_content = unit_by_name["orca_auto-bot@.service"].content
    assert "ProtectHome=read-only" in bot_content
    assert f"ReadWritePaths={repo.resolve(strict=False) / 'admission'}" in bot_content
    assert unit_by_name["orca_auto-runtime@.target"].destination == (
        unit_dir.resolve(strict=False) / "orca_auto-runtime@.target"
    )


def test_systemd_read_write_paths_include_default_admission_for_workflow_config(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    (repo / "workflow_runs").mkdir()
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'workflow_runs'}",
                "messenger:",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '123'",
                "orca:",
                "  paths:",
                f"    orca_executable: {repo / 'orca'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_start=True,
        is_root=lambda: True,
    )

    unit_by_name = {unit.name: unit for unit in plan.units}
    worker_content = unit_by_name["orca_auto-queue-worker@.service"].content
    assert (
        "ReadWritePaths="
        f"{repo.resolve(strict=False) / 'workflow_runs' / '.admission'} "
        f"{repo.resolve(strict=False) / 'workflow_runs'}"
    ) in worker_content


def test_systemd_rejects_orca_scoped_admission_override(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "scheduler:",
                f"  admission_root: {repo / 'admission'}",
                "orca:",
                "  scheduler:",
                f"    admission_root: {repo / 'orca_admission'}",
                "messenger:",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '123'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot override the shared top-level scheduler"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            is_root=lambda: True,
        )


def test_systemd_rejects_non_mapping_orca_scheduler(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "scheduler:",
                "  max_active_simulations: 1",
                "orca:",
                "  scheduler: disabled",
                "messenger:",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '123'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="orca.scheduler must be a mapping"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            is_root=lambda: True,
        )


def test_systemd_read_write_paths_omit_invalid_runs_root(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                "runs_root: './runs'",
                "messenger:",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '123'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_enable=True,
        is_root=lambda: True,
    )

    unit_by_name = {unit.name: unit for unit in plan.units}
    worker_content = unit_by_name["orca_auto-queue-worker@.service"].content
    # A cwd-derived path must not be granted; the placeholder comment stays.
    assert "ReadWritePaths=" not in worker_content
    assert "# ReadWritePaths omitted" in worker_content


def test_rendered_systemd_units_pass_systemd_analyze_verify(tmp_path: Path) -> None:
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is not installed")
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=unit_dir,
        is_root=lambda: True,
    )
    unit_dir.mkdir()
    for unit in plan.units:
        unit.destination.write_text(unit.content, encoding="utf-8")

    result = subprocess.run(
        ["systemd-analyze", "verify", *(str(unit.destination) for unit in plan.units)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_build_systemd_install_plan_rejects_unsafe_user(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)

    with pytest.raises(ValueError, match="--user must be a Linux account name"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice/../../evil",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            is_root=lambda: True,
        )


def test_build_systemd_install_plan_rejects_paths_that_break_unit_syntax(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path / "with space")

    with pytest.raises(ValueError, match="--repo must not contain whitespace"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            is_root=lambda: True,
        )


def test_systemd_read_write_paths_reject_whitespace_from_config(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'workflow runs'}",
                "scheduler:",
                f"  admission_root: {repo / 'admission'}",
                "messenger:",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '123'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ReadWritePaths path must not contain whitespace"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            is_root=lambda: True,
        )


def test_build_systemd_install_plan_worker_only_enables_worker_service(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        worker_only=True,
        no_start=True,
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-queue-worker@alice.service"
    assert plan.commands == (
        ("systemctl", "daemon-reload"),
        ("systemctl", "disable", "orca_auto-runtime@alice.target"),
        ("systemctl", "enable", "orca_auto-queue-worker@alice.service"),
    )
    assert plan.requires_inactive_preflight is True
    assert plan.live_transition is False
    assert any("--no-start" in warning for warning in plan.warnings)


def test_build_systemd_install_plan_worker_only_disables_opposite_before_selected_enable(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        worker_only=True,
        is_root=lambda: True,
    )

    assert plan.commands == (
        ("systemctl", "daemon-reload"),
        ("systemctl", "disable", "orca_auto-runtime@alice.target"),
        ("systemctl", "enable", "orca_auto-queue-worker@alice.service"),
    )
    assert plan.live_transition is True


def test_build_systemd_install_plan_no_enable_does_not_change_selected_or_live_mode(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_enable=True,
        is_root=lambda: True,
    )

    assert plan.enabled_unit is None
    assert plan.commands == (("systemctl", "daemon-reload"),)
    assert plan.requires_inactive_preflight is True
    assert plan.live_transition is False
    assert any("--no-enable" in warning for warning in plan.warnings)


def test_cmd_systemd_install_writes_units_and_runs_commands(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    runner = _SystemdStateRunner(workflow_active=True)

    args = Namespace(
        target_user="alice",
        repo=str(repo),
        config=str(config_path),
        unit_dir=str(unit_dir),
        worker_only=False,
        no_enable=False,
        no_start=False,
        dry_run=False,
        no_sudo=True,
    )

    result = cli_systemd_apply.cmd_systemd_install(
        args,
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result == 0
    assert runner.commands[:9] == [
        ("systemctl", "is-active", runner.runtime),
        ("systemctl", "is-active", runner.worker),
        ("systemctl", "is-active", runner.bot),
        ("systemctl", "is-active", runner.workflow),
        ("systemctl", "stop", runner.workflow),
        ("systemctl", "is-active", runner.runtime),
        ("systemctl", "is-active", runner.worker),
        ("systemctl", "is-active", runner.bot),
        ("systemctl", "is-active", runner.workflow),
    ]
    assert runner.commands[9:12] == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "disable", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "enable", "orca_auto-runtime@alice.target"),
    ]
    assert ("systemctl", "start", "orca_auto-runtime@alice.target") in runner.commands
    assert runner.commands[-2:] == [
        ("systemctl", "start", "orca_auto-workflow-worker@alice.service"),
        ("systemctl", "is-active", "orca_auto-workflow-worker@alice.service"),
    ]
    assert (unit_dir / "orca_auto-queue-worker@.service").exists()
    assert (unit_dir / "orca_auto-runtime@.target").exists()
    captured = capsys.readouterr().out
    assert "installed:" in captured
    assert "enabled: orca_auto-runtime@alice.target" in captured


@pytest.mark.parametrize("failure", ["snapshot", "stop", "inactive-check"])
def test_live_systemd_install_drain_failure_does_not_write_new_units(
    tmp_path: Path,
    failure: str,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    runner = _SystemdStateRunner(
        runtime_active=failure in {"stop", "inactive-check"},
        query_error_unit=(
            "orca_auto-workflow-worker@alice.service" if failure == "snapshot" else None
        ),
        stop_rc=7 if failure == "stop" else 0,
        stuck_active_unit=("orca_auto-bot@alice.service" if failure == "inactive-check" else None),
    )

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(unit_dir),
            worker_only=False,
            no_enable=False,
            no_start=False,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result != 0
    assert not unit_dir.exists()
    assert not any(
        command[1] in {"daemon-reload", "disable", "enable"} for command in runner.commands
    )


def test_fresh_install_skips_stop_and_reset_for_absent_units(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    runner = _SystemdStateRunner()
    runner.states = {unit: "not-found" for unit in runner.states}

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(tmp_path / "units"),
            worker_only=False,
            no_enable=False,
            no_start=False,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result == 0
    assert not any(command[1] in {"stop", "reset-failed"} for command in runner.commands)
    assert ("systemctl", "start", runner.runtime) in runner.commands


def test_live_install_treats_failed_workflow_unit_as_stopped(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    runner = _SystemdStateRunner()
    runner.states[runner.workflow] = "failed"

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(tmp_path / "units"),
            worker_only=False,
            no_enable=False,
            no_start=False,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result == 0
    assert ("systemctl", "start", runner.workflow) not in runner.commands


@pytest.mark.parametrize(
    ("no_enable", "expected_commands"),
    [
        (True, [("systemctl", "daemon-reload")]),
        (
            False,
            [
                ("systemctl", "daemon-reload"),
                ("systemctl", "disable", "orca_auto-queue-worker@alice.service"),
                ("systemctl", "enable", "orca_auto-runtime@alice.target"),
            ],
        ),
    ],
)
def test_cmd_systemd_install_no_start_or_no_enable_skips_live_transition(
    tmp_path: Path,
    no_enable: bool,
    expected_commands: list[tuple[str, ...]],
) -> None:
    repo, config_path = _make_repo(tmp_path)
    runner = _SystemdStateRunner()

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(tmp_path / "units"),
            worker_only=False,
            no_enable=no_enable,
            no_start=not no_enable,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result == 0
    assert runner.commands == [
        ("systemctl", "is-active", runner.runtime),
        ("systemctl", "is-active", runner.worker),
        ("systemctl", "is-active", runner.bot),
        ("systemctl", "is-active", runner.workflow),
        *expected_commands,
    ]
    assert not any(command[1] in {"stop", "start"} for command in runner.commands)


@pytest.mark.parametrize(
    ("no_enable", "no_start"),
    [(True, False), (False, True)],
)
def test_cmd_systemd_install_offline_mode_refuses_active_unit_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    no_enable: bool,
    no_start: bool,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    runner = _SystemdStateRunner(runtime_active=True)

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(unit_dir),
            worker_only=False,
            no_enable=no_enable,
            no_start=no_start,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result == 1
    assert not unit_dir.exists()
    assert runner.commands == [
        ("systemctl", "is-active", runner.runtime),
        ("systemctl", "is-active", runner.worker),
        ("systemctl", "is-active", runner.bot),
        ("systemctl", "is-active", runner.workflow),
    ]
    assert "refusing to write systemd units" in capsys.readouterr().err


def test_cmd_systemd_install_offline_mode_refuses_query_error_before_writing(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    runner = _SystemdStateRunner(query_error_unit="orca_auto-bot@alice.service")

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(unit_dir),
            worker_only=False,
            no_enable=False,
            no_start=True,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=runner, is_root=lambda: True),
    )

    assert result == 1
    assert not unit_dir.exists()
    assert len(runner.commands) == 4


def test_cmd_systemd_install_dry_run_does_not_write_units(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"

    args = Namespace(
        target_user="alice",
        repo=str(repo),
        config=str(config_path),
        unit_dir=str(unit_dir),
        worker_only=True,
        no_enable=False,
        no_start=False,
        dry_run=True,
        no_sudo=True,
    )

    result = cli_systemd_apply.cmd_systemd_install(
        args,
        deps=cli_systemd_apply.SystemdInstallCliDeps(is_root=lambda: True),
    )

    assert result == 0
    assert not unit_dir.exists()
    captured = capsys.readouterr().out
    assert "systemd install plan:" in captured
    assert "enable: orca_auto-queue-worker@alice.service" in captured
    assert "systemctl disable orca_auto-runtime@alice.target" in captured
    assert "systemctl enable orca_auto-queue-worker@alice.service" in captured
    assert "pre-write live drain (fail closed)" in captured
    assert "snapshot: systemctl is-active orca_auto-workflow-worker@alice.service" in captured
    assert "stop: systemctl stop orca_auto-runtime@alice.target" in captured
    assert "start: systemctl start orca_auto-queue-worker@alice.service" in captured
    assert "post-write live start (fail closed)" in captured


def test_cmd_systemd_install_offline_dry_run_displays_prewrite_preflight(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(config_path),
            unit_dir=str(unit_dir),
            worker_only=False,
            no_enable=False,
            no_start=True,
            dry_run=True,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(is_root=lambda: True),
    )

    assert result == 0
    assert not unit_dir.exists()
    captured = capsys.readouterr().out
    assert "pre-write inactive preflight (fail closed):" in captured
    for unit in systemd_plan.managed_runtime_units_for_user("alice"):
        assert f"require exit 3/inactive: systemctl is-active {unit}" in captured


def test_full_runtime_warns_when_telegram_is_not_configured(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "messenger:",
                "  telegram:",
                "    bot_token: ''",
                "    chat_id: ''",
                "orca:",
                "  paths:",
                f"    orca_executable: {repo / 'orca'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-queue-worker@alice.service"
    assert any("Telegram is not fully configured" in warning for warning in plan.warnings)


def test_telegram_group_without_operator_allowlist_stays_worker_only(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "messenger:",
                "  provider: telegram",
                "  telegram:",
                "    bot_token: token",
                "    chat_id: '-100123'",
                "    allowed_user_ids: []",
                "orca:",
                "  paths:",
                f"    orca_executable: {repo / 'orca'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_start=True,
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-queue-worker@alice.service"
    assert any("allowed_user_ids" in warning for warning in plan.warnings)


def test_discord_bot_credentials_enable_full_runtime_with_neutral_entrypoint(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "messenger:",
                "  provider: discord",
                "  discord:",
                "    bot_token: token",
                "    channel_ids:",
                "      - '100'",
                "    default_channel_id: '200'",
                "    allowed_user_ids:",
                "      - '7'",
                "orca:",
                "  paths:",
                f"    orca_executable: {repo / 'orca'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_start=True,
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-runtime@alice.target"
    bot_unit = next(unit for unit in plan.units if unit.name == "orca_auto-bot@.service")
    assert "-m orca_auto.flow.bot.runner" in bot_unit.content
    assert "orca_auto.flow.telegram.bot" not in bot_unit.content
    assert not any("notification-only" in warning for warning in plan.warnings)


def test_discord_without_operator_allowlist_stays_worker_only(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "messenger:",
                "  provider: discord",
                "  discord:",
                "    bot_token: token",
                "    channel_ids: ['100']",
                "    default_channel_id: '200'",
                "    allowed_user_ids: []",
                "orca:",
                "  paths:",
                f"    orca_executable: {repo / 'orca'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_start=True,
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-queue-worker@alice.service"
    assert any("allowed_user_ids" in warning for warning in plan.warnings)


def test_discord_notification_only_stays_worker_only_even_with_telegram_credentials(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'orca_runs'}",
                "messenger:",
                "  provider: discord",
                "  telegram:",
                "    bot_token: telegram-token",
                "    chat_id: telegram-chat",
                "  discord:",
                "    bot_token: discord-token",
                '    default_channel_id: "123456789012345678"',
                "orca:",
                "  paths:",
                f"    orca_executable: {repo / 'orca'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_start=True,
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-queue-worker@alice.service"
    assert any("interactive bot settings are incomplete" in warning for warning in plan.warnings)


@pytest.mark.parametrize("content", [None, "messenger: [\n"])
def test_live_systemd_install_rejects_missing_or_invalid_runtime_config(
    tmp_path: Path,
    content: str | None,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    if content is None:
        config_path.unlink()
    else:
        config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="runtime config preflight failed"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            is_root=lambda: True,
        )


def test_no_start_rejects_staging_units_with_missing_config(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.unlink()

    with pytest.raises(ValueError, match="runtime config preflight failed"):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            no_start=True,
            is_root=lambda: True,
        )


def test_no_enable_allows_offline_unit_staging_with_missing_config(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.unlink()

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        no_enable=True,
        is_root=lambda: True,
    )

    assert any("config file does not exist" in warning for warning in plan.warnings)
    assert any("--no-enable" in warning for warning in plan.warnings)


def test_cmd_service_status_prints_compact_systemd_state(capsys: Any) -> None:
    states = {
        ("is-active", "orca_auto-runtime@alice.target"): "active",
        ("is-enabled", "orca_auto-runtime@alice.target"): "enabled",
        ("is-active", "orca_auto-queue-worker@alice.service"): "active",
        ("is-enabled", "orca_auto-queue-worker@alice.service"): "enabled",
        ("is-active", "orca_auto-workflow-worker@alice.service"): "inactive",
        ("is-enabled", "orca_auto-workflow-worker@alice.service"): "disabled",
        ("is-active", "orca_auto-bot@alice.service"): "inactive",
        ("is-enabled", "orca_auto-bot@alice.service"): "disabled",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        value = states[(argv[1], argv[2])]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "orca_auto service status for alice (full):" in output
    assert "Active" in output
    assert "Startup" not in output
    assert "Enabled" not in output
    assert "worker" in output
    assert "orca_auto-queue-worker@alice.service" in output
    assert "workflow" in output
    assert "orca_auto-workflow-worker@alice.service" in output
    assert "inactive" in output


def test_cmd_service_status_worker_only_requires_only_worker(capsys: Any) -> None:
    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            label="runtime",
            unit="orca_auto-runtime@alice.target",
            active="inactive",
            enabled="disabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="bot",
            unit="orca_auto-bot@alice.service",
            active="not-found",
            enabled="not-found",
        ),
    )

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "worker-only"
    assert payload["ok"] is True
    required = {item["label"] for item in payload["services"] if item["required"]}
    assert required == {"worker"}


def test_cmd_service_status_hides_runtime_managed_enabled_noise(
    capsys: Any,
) -> None:
    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            label="runtime",
            unit="orca_auto-runtime@alice.target",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="disabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="bot",
            unit="orca_auto-bot@alice.service",
            active="active",
            enabled="disabled",
        ),
    )

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "Startup" not in output
    assert "Enabled" not in output
    assert "via runtime" not in output
    assert "disabled" not in output


def test_cmd_service_status_emits_json(capsys: Any) -> None:
    states = {
        ("is-active", "orca_auto-runtime@alice.target"): "active",
        ("is-enabled", "orca_auto-runtime@alice.target"): "enabled",
        ("is-active", "orca_auto-queue-worker@alice.service"): "failed",
        ("is-enabled", "orca_auto-queue-worker@alice.service"): "enabled",
        ("is-active", "orca_auto-workflow-worker@alice.service"): "inactive",
        ("is-enabled", "orca_auto-workflow-worker@alice.service"): "disabled",
        ("is-active", "orca_auto-bot@alice.service"): "inactive",
        ("is-enabled", "orca_auto-bot@alice.service"): "disabled",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{states[(argv[1], argv[2])]}\n", stderr=""
        )

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None, json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    # A failed unit yields a non-zero exit even in JSON mode.
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_user"] == "alice"
    assert payload["ok"] is False
    worker = next(s for s in payload["services"] if s["label"] == "worker")
    assert worker["active"] == "failed"


def test_cmd_service_status_fails_when_systemctl_is_missing(capsys: Any) -> None:
    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(which=lambda name: None),
    )

    assert result == 1
    assert "systemctl is not available" in capsys.readouterr().err


def test_cmd_service_restart_prefers_runtime_when_enabled(capsys: Any) -> None:
    runner = _SystemdStateRunner(workflow_active=True)

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=runner,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert ("systemctl", "start", "orca_auto-runtime@alice.target") in runner.commands
    assert runner.commands[-2:] == [
        ("systemctl", "start", "orca_auto-workflow-worker@alice.service"),
        ("systemctl", "is-active", "orca_auto-workflow-worker@alice.service"),
    ]
    assert "Safe runtime replacement completed successfully" in capsys.readouterr().out


def test_cmd_service_restart_falls_back_to_worker_when_runtime_is_disabled() -> None:
    runner = _SystemdStateRunner(runtime_enabled=False)

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=runner,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert ("systemctl", "start", "orca_auto-queue-worker@alice.service") in runner.commands
    assert ("systemctl", "start", "orca_auto-workflow-worker@alice.service") not in (
        runner.commands
    )


def test_cmd_service_restart_fails_closed_on_runtime_query_error() -> None:
    runner = _SystemdStateRunner(query_error_unit="orca_auto-runtime@alice.target")

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=runner,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert not any(command[1] == "stop" for command in runner.commands)


def test_cmd_service_restart_rejects_matching_state_with_wrong_return_code() -> None:
    runner = _SystemdStateRunner()

    def misleading_run(
        argv: tuple[str, ...] | list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        systemctl = tuple(argv[1:]) if argv[0] == "sudo" else tuple(argv)
        if systemctl == ("systemctl", "is-active", runner.runtime):
            return subprocess.CompletedProcess(argv, 5, stdout="active\n", stderr="")
        return runner(argv, check=check, stdout=stdout, stderr=stderr, text=text)

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=misleading_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert not any(command[1] == "stop" for command in runner.commands)


def test_cmd_service_restart_prefers_enabled_full_runtime() -> None:
    runner = _SystemdStateRunner(runtime_enabled=True)
    runner.enabled[runner.worker] = "enabled"

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=runner,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert ("systemctl", "start", runner.runtime) in runner.commands


def test_install_and_restart_serialize_mode_query_through_restore(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    runner = _SystemdStateRunner(runtime_active=True, runtime_enabled=True)
    install_stop_entered = threading.Event()
    release_install = threading.Event()
    restart_started = threading.Event()
    blocked_once = False
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    def blocking_run(
        argv: tuple[str, ...] | list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal blocked_once
        systemctl = tuple(argv[1:]) if argv[0] == "sudo" else tuple(argv)
        if len(systemctl) > 1 and systemctl[1] == "stop" and not blocked_once:
            blocked_once = True
            install_stop_entered.set()
            if not release_install.wait(timeout=2.0):
                raise AssertionError("install transition was not released")
        return runner(argv, check=check, stdout=stdout, stderr=stderr, text=text)

    def install() -> None:
        try:
            results["install"] = cli_systemd_apply.cmd_systemd_install(
                Namespace(
                    target_user="alice",
                    repo=str(repo),
                    config=str(config_path),
                    unit_dir=str(unit_dir),
                    worker_only=True,
                    no_enable=False,
                    no_start=False,
                    dry_run=False,
                    no_sudo=True,
                ),
                deps=cli_systemd_apply.SystemdInstallCliDeps(
                    run=blocking_run,
                    is_root=lambda: True,
                ),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def restart() -> None:
        restart_started.set()
        try:
            results["restart"] = cli_systemd_status.cmd_service_restart(
                Namespace(target_user=None),
                deps=cli_systemd_status.ServiceCliDeps(
                    default_service_user=lambda: "alice",
                    is_root=lambda: True,
                    run=blocking_run,
                    which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
                ),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    install_thread = threading.Thread(target=install, daemon=True)
    restart_thread = threading.Thread(target=restart, daemon=True)
    install_thread.start()
    assert install_stop_entered.wait(timeout=1.0)
    commands_while_install_holds_lock = list(runner.commands)
    restart_thread.start()
    assert restart_started.wait(timeout=1.0)
    restart_thread.join(timeout=0.2)
    assert restart_thread.is_alive()
    assert runner.commands == commands_while_install_holds_lock

    release_install.set()
    install_thread.join(timeout=2.0)
    restart_thread.join(timeout=2.0)

    assert not install_thread.is_alive()
    assert not restart_thread.is_alive()
    assert errors == []
    assert results == {"install": 0, "restart": 0}
    assert ("systemctl", "start", runner.runtime) not in runner.commands
    assert runner.commands.count(("systemctl", "start", runner.worker)) == 2


def test_direct_runtime_replacement_lock_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_systemd_apply, "SYSTEMD_TRANSITION_LOCK_TIMEOUT_SECONDS", 0.05)
    ready = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_transition() -> None:
        try:
            with cli_systemd_apply.systemd_transition_lock("alice"):
                ready.set()
                release.wait(timeout=2.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    holder = threading.Thread(target=hold_transition, daemon=True)
    holder.start()
    assert ready.wait(timeout=1.0)
    runner = _SystemdStateRunner()
    try:
        result = cli_systemd_apply.replace_selected_systemd_runtime(
            "alice",
            runner.worker,
            use_sudo=False,
            run=runner,
        )
    finally:
        release.set()
        holder.join(timeout=2.0)

    assert result == 1
    assert not holder.is_alive()
    assert errors == []
    assert runner.commands == []
    assert "could not acquire systemd transition lock" in capsys.readouterr().err


def test_systemd_transition_socket_identity_is_euid_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = cli_systemd_apply._systemd_transition_socket_address("alice")
    monkeypatch.setattr(cli_systemd_apply.os, "geteuid", lambda: 0)

    assert cli_systemd_apply._systemd_transition_socket_address("alice") == address
    assert cli_systemd_apply._systemd_transition_socket_address("bob") != address
    assert address.startswith(b"\0orca_auto-sd-transition-v1-")
    assert len(address) <= 107


def test_nested_systemd_transition_lock_keeps_outer_socket_held() -> None:
    address = cli_systemd_apply._systemd_transition_socket_address("nested_lock_test")
    other_address = cli_systemd_apply._systemd_transition_socket_address("other_lock_test")
    thread_locks = cli_systemd_apply._current_thread_systemd_transition_locks()

    with cli_systemd_apply.systemd_transition_lock("nested_lock_test"):
        outer_socket = thread_locks[address].socket
        outer_descriptor = outer_socket.fileno()
        assert outer_descriptor >= 0
        assert not outer_socket.get_inheritable()

        with cli_systemd_apply.systemd_transition_lock("nested_lock_test"):
            assert thread_locks[address].socket is outer_socket
            assert outer_socket.fileno() == outer_descriptor

        assert thread_locks[address].socket is outer_socket
        assert outer_socket.fileno() == outer_descriptor

        with cli_systemd_apply.systemd_transition_lock("other_lock_test"):
            assert thread_locks[other_address].socket is not outer_socket
            assert outer_socket.fileno() == outer_descriptor

    assert address not in thread_locks
    assert other_address not in thread_locks
    assert outer_socket.fileno() == -1


def test_subprocess_systemd_transition_lock_timeout_runs_no_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_user = "socket_lock_test"
    child_code = (
        "import sys\n"
        "from orca_auto.cli_systemd_apply import systemd_transition_lock\n"
        f"with systemd_transition_lock({target_user!r}):\n"
        "    print('ready', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    assert child.stderr is not None
    try:
        ready_streams, _, _ = select.select([child.stdout], [], [], 2.0)
        assert ready_streams, child.stderr.read() if child.poll() is not None else ""
        assert child.stdout.readline().strip() == "ready"

        monkeypatch.setattr(
            cli_systemd_apply,
            "SYSTEMD_TRANSITION_LOCK_TIMEOUT_SECONDS",
            0.05,
        )
        runner = _SystemdStateRunner()
        result = cli_systemd_apply.replace_selected_systemd_runtime(
            target_user,
            f"orca_auto-queue-worker@{target_user}.service",
            use_sudo=False,
            run=runner,
        )

        assert result == 1
        assert runner.commands == []
        assert "could not acquire systemd transition lock" in capsys.readouterr().err
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2.0)

    assert child.returncode == 0, child.stderr.read()


def test_cmd_service_restart_uses_sudo_for_non_root_user() -> None:
    runner = _SystemdStateRunner()

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
            is_root=lambda: False,
            run=runner,
            which=lambda name: f"/usr/bin/{name}" if name in {"systemctl", "sudo"} else None,
        ),
    )

    assert result == 0
    assert runner.commands
    assert all(command[0] == "sudo" for command in runner.commands)
    assert (
        "sudo",
        "systemctl",
        "start",
        "orca_auto-runtime@alice.target",
    ) in runner.commands


def test_cmd_service_restart_stops_when_reset_failed_cannot_clear_start_limit() -> None:
    runner = _SystemdStateRunner(reset_rc=5)
    runner.states[runner.worker] = "failed"

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            restart_unit_for_user=lambda target_user, run: (
                f"orca_auto-runtime@{target_user}.target"
            ),
            is_root=lambda: True,
            run=runner,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 5
    assert runner.commands[-1] == (
        "systemctl",
        "reset-failed",
        "orca_auto-queue-worker@alice.service",
    )
    assert not any(command[1] == "start" for command in runner.commands)


def _assert_supervised_graph_stopped(runner: _SystemdStateRunner) -> None:
    assert runner.commands[-5:] == [
        ("systemctl", "stop", runner.runtime, runner.worker, runner.bot, runner.workflow),
        ("systemctl", "is-active", runner.runtime),
        ("systemctl", "is-active", runner.worker),
        ("systemctl", "is-active", runner.bot),
        ("systemctl", "is-active", runner.workflow),
    ]
    assert set(runner.states.values()) == {"inactive"}


def test_safe_runtime_replacement_stops_on_stop_failure() -> None:
    runner = _SystemdStateRunner(workflow_active=True, stop_rc=7)

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 7
    assert runner.commands == [
        ("systemctl", "is-active", runner.runtime),
        ("systemctl", "is-active", runner.worker),
        ("systemctl", "is-active", runner.bot),
        ("systemctl", "is-active", runner.workflow),
        ("systemctl", "stop", runner.workflow),
    ]


def test_safe_runtime_replacement_stops_when_a_managed_unit_remains_active() -> None:
    runner = _SystemdStateRunner(
        runtime_active=True,
        stuck_active_unit="orca_auto-bot@alice.service",
    )

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 1
    assert runner.commands[-1] == ("systemctl", "is-active", runner.bot)
    assert not any(command[1] in {"reset-failed", "start"} for command in runner.commands)


def test_safe_runtime_replacement_stops_when_selected_start_fails() -> None:
    runner = _SystemdStateRunner(
        workflow_active=True,
        start_failures={"orca_auto-runtime@alice.target": 8},
    )

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 8
    assert ("systemctl", "start", runner.runtime) in runner.commands
    assert ("systemctl", "start", runner.workflow) not in runner.commands
    _assert_supervised_graph_stopped(runner)


def test_safe_runtime_replacement_rejects_inactive_selected_unit_after_start() -> None:
    runner = _SystemdStateRunner(
        workflow_active=True,
        start_inactive_units={"orca_auto-runtime@alice.target"},
    )

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 1
    assert ("systemctl", "is-active", runner.runtime) in runner.commands
    assert ("systemctl", "start", runner.workflow) not in runner.commands
    _assert_supervised_graph_stopped(runner)


def test_safe_runtime_replacement_rejects_inactive_full_runtime_child() -> None:
    runner = _SystemdStateRunner(
        workflow_active=True,
        start_inactive_units={"orca_auto-bot@alice.service"},
    )

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 1
    assert ("systemctl", "is-active", runner.bot) in runner.commands
    assert ("systemctl", "start", runner.workflow) not in runner.commands
    _assert_supervised_graph_stopped(runner)


def test_safe_runtime_replacement_reports_workflow_restore_failure() -> None:
    runner = _SystemdStateRunner(
        workflow_active=True,
        start_failures={"orca_auto-workflow-worker@alice.service": 9},
    )

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 9
    assert ("systemctl", "start", runner.workflow) in runner.commands
    _assert_supervised_graph_stopped(runner)


def test_safe_runtime_replacement_rejects_inactive_workflow_after_restore() -> None:
    runner = _SystemdStateRunner(
        workflow_active=True,
        start_inactive_units={"orca_auto-workflow-worker@alice.service"},
    )

    result = cli_systemd_apply.replace_selected_systemd_runtime(
        "alice",
        runner.runtime,
        use_sudo=False,
        run=runner,
    )

    assert result == 1
    _assert_supervised_graph_stopped(runner)


def _single_unit_plan(
    tmp_path: Path,
    *,
    use_sudo: bool = False,
    commands: tuple[tuple[str, ...], ...] = (),
    enabled_unit: str | None = None,
    live_transition: bool = False,
) -> systemd_plan.SystemdInstallPlan:
    python_path = tmp_path / ".venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    python_path.chmod(0o755)
    return systemd_plan.SystemdInstallPlan(
        target_user="alice",
        repo=tmp_path,
        config=tmp_path / "config" / "orca_auto.yaml",
        unit_dir=tmp_path / "units",
        units=(
            systemd_plan.RenderedUnit(
                name="orca_auto-test.service",
                destination=tmp_path / "units" / "orca_auto-test.service",
                content="[Unit]\nDescription=Test\n",
            ),
        ),
        commands=commands,
        enabled_unit=enabled_unit,
        use_sudo=use_sudo,
        warnings=(),
        live_transition=live_transition,
    )


def test_apply_systemd_install_plan_reports_direct_write_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _single_unit_plan(tmp_path)
    plan.unit_dir.write_text("not a directory", encoding="utf-8")

    assert cli_systemd_apply.apply_systemd_install_plan(plan) == 1
    assert "failed to write systemd units" in capsys.readouterr().err


def test_live_systemd_install_write_failure_keeps_old_graph_stopped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _SystemdStateRunner(runtime_active=True)
    plan = _single_unit_plan(
        tmp_path,
        enabled_unit=runner.worker,
        live_transition=True,
    )
    plan.unit_dir.write_text("not a directory", encoding="utf-8")

    assert cli_systemd_apply.apply_systemd_install_plan(plan, run=runner) == 1
    assert set(runner.states.values()) == {"inactive"}
    assert not any(command[1] == "start" for command in runner.commands)
    assert "supervised graph remains stopped" in capsys.readouterr().err


def test_apply_systemd_install_plan_rejects_missing_python_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _single_unit_plan(tmp_path)
    (tmp_path / ".venv" / "bin" / "python").unlink()
    commands: list[tuple[str, ...]] = []

    def fake_run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0)

    assert cli_systemd_apply.apply_systemd_install_plan(plan, run=fake_run) == 1
    assert not plan.unit_dir.exists()
    assert commands == []
    assert "run `make venv`" in capsys.readouterr().err


def test_apply_systemd_install_plan_requires_sudo_when_plan_uses_sudo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orca_auto.cli_systemd_apply.shutil.which", lambda name: None)

    assert (
        cli_systemd_apply.apply_systemd_install_plan(_single_unit_plan(tmp_path, use_sudo=True))
        == 1
    )
    assert "sudo is required to write system units" in capsys.readouterr().err


def test_apply_systemd_install_plan_stops_when_sudo_write_command_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "orca_auto.cli_systemd_apply.shutil.which",
        lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )

    def fake_run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 7)

    result = cli_systemd_apply.apply_systemd_install_plan(
        _single_unit_plan(tmp_path, use_sudo=True),
        run=fake_run,
    )

    assert result == 7
    assert commands == [("sudo", "mkdir", "-p", str(tmp_path / "units"))]


def test_apply_systemd_install_plan_disable_failure_does_not_enable_selected_mode(
    tmp_path: Path,
) -> None:
    runner = _SystemdStateRunner(runtime_enabled=True, disable_rc=8)
    plan = _single_unit_plan(
        tmp_path,
        commands=(
            ("systemctl", "daemon-reload"),
            ("systemctl", "disable", runner.runtime),
            ("systemctl", "enable", runner.worker),
        ),
        enabled_unit=runner.worker,
        live_transition=True,
    )

    assert cli_systemd_apply.apply_systemd_install_plan(plan, run=runner) == 8
    assert runner.commands[-2:] == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "disable", runner.runtime),
    ]
    assert runner.enabled[runner.runtime] == "enabled"
    assert runner.enabled[runner.worker] == "disabled"
    assert not any(command[1] == "start" for command in runner.commands)
    assert set(runner.states.values()) == {"inactive"}


def test_apply_systemd_install_plan_enable_failure_leaves_opposite_and_old_graph_stopped(
    tmp_path: Path,
) -> None:
    runner = _SystemdStateRunner(
        runtime_active=True,
        runtime_enabled=True,
        enable_rc=9,
    )
    plan = _single_unit_plan(
        tmp_path,
        commands=(
            ("systemctl", "daemon-reload"),
            ("systemctl", "disable", runner.runtime),
            ("systemctl", "enable", runner.worker),
        ),
        enabled_unit=runner.worker,
        live_transition=True,
    )

    assert cli_systemd_apply.apply_systemd_install_plan(plan, run=runner) == 9
    assert runner.commands[-3:] == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "disable", runner.runtime),
        ("systemctl", "enable", runner.worker),
    ]
    assert runner.enabled[runner.runtime] == "disabled"
    assert runner.enabled[runner.worker] == "disabled"
    assert set(runner.states.values()) == {"inactive"}
    assert not any(command[1] == "start" for command in runner.commands)


def test_run_command_uses_shared_systemd_argv_and_display(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(
        argv: tuple[str, ...],
        check: bool = False,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del check
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    command = ("systemctl", "daemon-reload")

    assert cli_systemd_apply._run_command(command, use_sudo=True, run=fake_run) == 0

    assert commands == [("sudo", "systemctl", "daemon-reload")]
    assert capsys.readouterr().out == (
        f"$ {systemd_plan._format_command(command, use_sudo=True)}\n"
    )


def test_cmd_service_status_returns_failure_when_any_unit_failed(capsys: Any) -> None:
    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            label="runtime",
            unit="orca_auto-runtime@alice.target",
            active="failed",
            enabled="enabled",
        ),
    )

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert "failed" in capsys.readouterr().out


@pytest.mark.parametrize("unhealthy_state", ["inactive", "not-found", "error: dbus down"])
def test_cmd_service_status_full_mode_rejects_any_non_active_required_unit(
    unhealthy_state: str,
) -> None:
    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            label="runtime",
            unit="orca_auto-runtime@alice.target",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="disabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="bot",
            unit="orca_auto-bot@alice.service",
            active=unhealthy_state,
            enabled="disabled",
        ),
    )

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1


def test_cmd_service_restart_requires_sudo_for_non_root_user(capsys: Any) -> None:
    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
            is_root=lambda: False,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 1
    assert "sudo is required to restart system services" in capsys.readouterr().err
