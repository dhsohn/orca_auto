from __future__ import annotations

import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli_systemd_apply, cli_systemd_status, systemd_plan


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "orca_auto"
    python_path = repo / ".venv" / "bin" / "python"
    config_path = repo / "config" / "orca_auto.yaml"
    python_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    python_path.chmod(0o755)
    config_path.write_text(
        "\n".join(
            [
                "scheduler:",
                f"  admission_root: {repo / 'admission'}",
                "workflow:",
                f"  root: {repo / 'workflow_runs'}",
                "orca:",
                "  runtime:",
                f"    allowed_root: {repo / 'orca_runs'}",
                f"    organized_root: {repo / 'orca_outputs'}",
                "telegram:",
                "  bot_token: token",
                "  chat_id: chat",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return repo, config_path


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
        ("systemctl", "enable", "--now", "orca_auto-runtime@alice.target"),
    )

    unit_by_name = {unit.name: unit for unit in plan.units}
    worker_content = unit_by_name["orca_auto-queue-worker@.service"].content
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
    assert (
        "ReadWritePaths="
        f"{repo.resolve(strict=False) / 'admission'} "
        f"{repo.resolve(strict=False) / 'workflow_runs'} "
        f"{repo.resolve(strict=False) / 'orca_runs'} "
        f"{repo.resolve(strict=False) / 'orca_outputs'}"
    ) in worker_content
    bot_content = unit_by_name["orca_auto-bot@.service"].content
    assert "ProtectHome=read-only" in bot_content
    assert f"ReadWritePaths={repo.resolve(strict=False) / 'admission'}" in bot_content
    assert unit_by_name["orca_auto-runtime@.target"].destination == (
        unit_dir.resolve(strict=False) / "orca_auto-runtime@.target"
    )


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
        ("systemctl", "enable", "orca_auto-queue-worker@alice.service"),
    )


def test_cmd_systemd_install_writes_units_and_runs_commands(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    commands: list[tuple[str, ...]] = []

    def _fake_run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0)

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
        deps=cli_systemd_apply.SystemdInstallCliDeps(run=_fake_run, is_root=lambda: True),
    )

    assert result == 0
    assert commands == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "--now", "orca_auto-runtime@alice.target"),
    ]
    assert (unit_dir / "orca_auto-queue-worker@.service").exists()
    assert (unit_dir / "orca_auto-runtime@.target").exists()
    captured = capsys.readouterr().out
    assert "installed:" in captured
    assert "enabled: orca_auto-runtime@alice.target" in captured


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
    assert "systemctl enable --now orca_auto-queue-worker@alice.service" in captured


def test_full_runtime_warns_when_telegram_is_not_configured(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text("telegram:\n  bot_token: ''\n  chat_id: ''\n", encoding="utf-8")

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=tmp_path / "units",
        is_root=lambda: True,
    )

    assert plan.enabled_unit == "orca_auto-queue-worker@alice.service"
    assert any("Telegram is not fully configured" in warning for warning in plan.warnings)


def test_cmd_service_status_prints_compact_systemd_state(capsys: Any) -> None:
    states = {
        ("is-active", "orca_auto-runtime@alice.target"): "active",
        ("is-enabled", "orca_auto-runtime@alice.target"): "enabled",
        ("is-active", "orca_auto-queue-worker@alice.service"): "active",
        ("is-enabled", "orca_auto-queue-worker@alice.service"): "enabled",
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

    assert result == 0
    output = capsys.readouterr().out
    assert "orca_auto service status for alice:" in output
    assert "Active" in output
    assert "Startup" not in output
    assert "Enabled" not in output
    assert "worker" in output
    assert "orca_auto-queue-worker@alice.service" in output
    assert "inactive" in output


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
        if argv[1] == "is-active":
            return subprocess.CompletedProcess(argv, 3, stdout="inactive\n", stderr="")
        if argv[1] == "is-enabled":
            return subprocess.CompletedProcess(argv, 0, stdout="enabled\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert commands[-1] == ("systemctl", "restart", "orca_auto-runtime@alice.target")
    assert "Restarting orca_auto-runtime@alice.target" in capsys.readouterr().out


def test_cmd_service_restart_falls_back_to_worker_when_runtime_is_disabled() -> None:
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
        if argv[1] == "is-active":
            return subprocess.CompletedProcess(argv, 3, stdout="inactive\n", stderr="")
        if argv[1] == "is-enabled":
            return subprocess.CompletedProcess(argv, 1, stdout="disabled\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert commands[-1] == ("systemctl", "restart", "orca_auto-queue-worker@alice.service")


def test_cmd_service_restart_uses_sudo_for_non_root_user() -> None:
    commands: list[tuple[str, ...]] = []

    def _fake_run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0)

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
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
    assert commands == [("sudo", "systemctl", "restart", "orca_auto-runtime@alice.target")]


def _single_unit_plan(
    tmp_path: Path,
    *,
    use_sudo: bool = False,
    commands: tuple[tuple[str, ...], ...] = (),
) -> systemd_plan.SystemdInstallPlan:
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
        enabled_unit=None,
        use_sudo=use_sudo,
        warnings=(),
    )


def test_apply_systemd_install_plan_reports_direct_write_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _single_unit_plan(tmp_path)
    plan.unit_dir.write_text("not a directory", encoding="utf-8")

    assert cli_systemd_apply.apply_systemd_install_plan(plan) == 1
    assert "failed to write systemd units" in capsys.readouterr().err


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
