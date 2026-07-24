from __future__ import annotations

import json
import os
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
                "  discord:",
                "    bot_token: token",
                "    default_channel_id: '123'",
                "orca:",
                "  paths:",
                f"    orca_executable: {orca_executable}",
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
        ("systemctl", "enable", "orca_auto-runtime@alice.target"),
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-runtime@alice.target"),
        ("systemctl", "is-active", "--quiet", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "disable", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "disable", "orca_auto-engine-workers@alice.target"),
    )

    unit_by_name = {unit.name: unit for unit in plan.units}
    worker_content = unit_by_name["orca_auto-queue-worker@.service"].content
    workflow_worker_content = unit_by_name["orca_auto-workflow-worker@.service"].content
    assert "queue worker --app orca" in worker_content
    assert "--app xtb_md" not in worker_content
    assert "queue worker --app workflow" in workflow_worker_content
    assert "orca_auto-xtb-md-worker@.service" not in unit_by_name
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
    engine_target = unit_by_name["orca_auto-engine-workers@.target"].content
    assert "Wants=orca_auto-queue-worker@%i.service" in engine_target
    assert "Wants=orca_auto-xtb-md-worker@%i.service" not in engine_target
    runtime_target = unit_by_name["orca_auto-runtime@.target"].content
    assert "Wants=orca_auto-engine-workers@%i.target" in runtime_target
    assert "Wants=orca_auto-queue-worker@%i.service" not in runtime_target
    assert unit_by_name["orca_auto-runtime@.target"].destination == (
        unit_dir.resolve(strict=False) / "orca_auto-runtime@.target"
    )


def test_systemd_read_write_paths_include_default_admission_for_workflow_config(
    tmp_path: Path,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f"runs_root: {repo / 'workflow_runs'}",
                "messenger:",
                "  discord:",
                "    bot_token: token",
                "    default_channel_id: '123'",
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
                "  discord:",
                "    bot_token: token",
                "    default_channel_id: '123'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown orca config fields are not supported"):
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
                "  discord:",
                "    bot_token: token",
                "    default_channel_id: '123'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown orca config fields are not supported"):
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
                "  discord:",
                "    bot_token: token",
                "    default_channel_id: '123'",
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
                "  discord:",
                "    bot_token: token",
                "    default_channel_id: '123'",
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


def test_build_systemd_install_plan_worker_only_enables_engine_target(tmp_path: Path) -> None:
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

    assert plan.enabled_unit == "orca_auto-engine-workers@alice.target"
    assert plan.commands == (
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "disable", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "disable", "orca_auto-runtime@alice.target"),
    )
    assert any("--no-start" in warning for warning in plan.warnings)


def test_build_systemd_install_plan_worker_only_stops_runtime_then_restarts_worker(
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
        ("systemctl", "enable", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "is-active", "--quiet", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "disable", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "disable", "orca_auto-runtime@alice.target"),
    )


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
    assert any("--no-enable" in warning for warning in plan.warnings)


def test_cmd_systemd_install_writes_units_and_runs_commands(
    tmp_path: Path,
    capsys: Any,
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unit_dir = tmp_path / "units"
    runner = _FakeSudoSystemd(unit_dir)

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
    assert runner.enabled == {
        "orca_auto-runtime@alice.target": "enabled",
        "orca_auto-engine-workers@alice.target": "disabled",
        "orca_auto-queue-worker@alice.service": "disabled",
    }
    assert set(unit for unit, state in runner.active.items() if state == "active") == set(
        runner.active
    )
    assert (unit_dir / "orca_auto-queue-worker@.service").exists()
    assert not (unit_dir / "orca_auto-xtb-md-worker@.service").exists()
    assert (unit_dir / "orca_auto-engine-workers@.target").exists()
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
    assert "enable: orca_auto-engine-workers@alice.target" in captured
    assert "systemctl disable orca_auto-runtime@alice.target" in captured
    assert "systemctl enable orca_auto-engine-workers@alice.target" in captured
    assert "systemctl reset-failed orca_auto-queue-worker@alice.service" in captured
    assert "systemctl restart orca_auto-engine-workers@alice.target" in captured
    assert "systemctl is-active --quiet orca_auto-queue-worker@alice.service" in captured


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


def test_no_start_rejects_missing_runtime_config_before_enabling(tmp_path: Path) -> None:
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


def test_no_enable_allows_staging_units_with_missing_config(tmp_path: Path) -> None:
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
        ("is-active", "orca_auto-engine-workers@alice.target"): "active",
        ("is-enabled", "orca_auto-engine-workers@alice.target"): "disabled",
        ("is-active", "orca_auto-queue-worker@alice.service"): "active",
        ("is-enabled", "orca_auto-queue-worker@alice.service"): "disabled",
        ("is-active", "orca_auto-workflow-worker@alice.service"): "inactive",
        ("is-enabled", "orca_auto-workflow-worker@alice.service"): "disabled",
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
            label="engines",
            unit="orca_auto-engine-workers@alice.target",
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
            label="workflow",
            unit="orca_auto-workflow-worker@alice.service",
            active="inactive",
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

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "worker-only"
    assert payload["ok"] is True
    required = {item["label"] for item in payload["services"] if item["required"]}
    assert required == {"engines", "worker"}


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
            label="engines",
            unit="orca_auto-engine-workers@alice.target",
            active="active",
            enabled="disabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="disabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="workflow",
            unit="orca_auto-workflow-worker@alice.service",
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
        ("is-active", "orca_auto-engine-workers@alice.target"): "active",
        ("is-enabled", "orca_auto-engine-workers@alice.target"): "disabled",
        ("is-active", "orca_auto-queue-worker@alice.service"): "failed",
        ("is-enabled", "orca_auto-queue-worker@alice.service"): "disabled",
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
    assert commands[-2:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-runtime@alice.target"),
    ]
    assert "Restarting orca_auto-runtime@alice.target" in capsys.readouterr().out


def test_cmd_service_restart_falls_back_to_engine_target_when_runtime_is_disabled() -> None:
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
    assert commands[-2:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
    ]


def test_cmd_service_restart_prefers_enabled_engine_target_over_active_runtime() -> None:
    commands: list[tuple[str, ...]] = []
    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            "runtime", "orca_auto-runtime@alice.target", "active", "disabled"
        ),
        cli_systemd_status.ServiceUnitStatus(
            "engines", "orca_auto-engine-workers@alice.target", "active", "enabled"
        ),
        cli_systemd_status.ServiceUnitStatus(
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

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert cli_systemd_status._selected_service_mode(statuses) == "worker-only"
    assert result == 0
    assert not any(command[1] == "is-active" for command in commands)
    assert commands[-2:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
    ]


def test_cmd_service_restart_uses_active_runtime_only_when_enablement_is_unreadable() -> None:
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
            return subprocess.CompletedProcess(argv, 0, stdout="loaded\n", stderr="")
        if argv[1] == "is-enabled":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="query failed\n")
        if argv[1] == "is-active":
            return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
            is_root=lambda: True,
            run=_fake_run,
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
        ),
    )

    assert result == 0
    assert commands[-2:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-runtime@alice.target"),
    ]


def test_cmd_service_restart_directs_legacy_install_to_current_installer(
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
        pytest.fail(f"legacy detection must stop before state mutation: {argv}")

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
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
    assert commands == [
        ("sudo", "systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("sudo", "systemctl", "restart", "orca_auto-runtime@alice.target"),
    ]


def test_cmd_service_restart_stops_when_reset_failed_cannot_clear_start_limit() -> None:
    commands: list[tuple[str, ...]] = []

    def _fake_run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 5)

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
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


def _single_unit_plan(
    tmp_path: Path,
    *,
    use_sudo: bool = False,
    commands: tuple[tuple[str, ...], ...] = (),
    enabled_unit: str | None = None,
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
                name="orca_auto-queue-worker@.service",
                destination=tmp_path / "units" / "orca_auto-queue-worker@.service",
                content="[Unit]\nDescription=Test\n",
            ),
        ),
        commands=commands,
        enabled_unit=enabled_unit,
        use_sudo=use_sudo,
        warnings=(),
    )


class _FakeSudoSystemd:
    def __init__(
        self,
        unit_dir: Path,
        *,
        enabled: dict[str, str] | None = None,
        active: dict[str, str] | None = None,
        failures: dict[tuple[str, ...], list[int]] | None = None,
    ) -> None:
        user = "alice"
        self.unit_dir = unit_dir
        self.commands: list[tuple[str, ...]] = []
        self.enabled = {
            f"orca_auto-runtime@{user}.target": "disabled",
            f"orca_auto-engine-workers@{user}.target": "disabled",
            f"orca_auto-queue-worker@{user}.service": "disabled",
        }
        self.enabled.update(enabled or {})
        self.active = {
            f"orca_auto-runtime@{user}.target": "inactive",
            f"orca_auto-engine-workers@{user}.target": "inactive",
            f"orca_auto-queue-worker@{user}.service": "inactive",
        }
        self.active.update(active or {})
        self.failures = failures or {}

    def _validated_mutation_path(self, value: str) -> Path:
        path = Path(value)
        absolute = Path(os.path.abspath(path))
        unit_absolute = Path(os.path.abspath(self.unit_dir))
        if absolute != unit_absolute and unit_absolute not in absolute.parents:
            pytest.fail(f"fake runner refused mutation outside unit dir: {path}")
        return path

    def _systemctl(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        action = argv[1]
        if action == "is-enabled":
            unit = argv[-1]
            if unit not in self.enabled:
                pytest.fail(f"unregistered enable-state query: {command}")
            state = self.enabled[unit]
            rc = 0 if state in {"enabled", "enabled-runtime"} else 1
            return subprocess.CompletedProcess(argv, rc, stdout=f"{state}\n", stderr="")
        if action == "show":
            unit = argv[-1]
            if argv[2:4] != ["--property=ActiveState", "--value"]:
                pytest.fail(f"unsupported show query: {command}")
            if unit not in self.active:
                pytest.fail(f"unregistered active-state query: {command}")
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.active[unit]}\n", stderr="")
        if action == "is-active":
            unit = argv[-1]
            if unit not in self.active:
                pytest.fail(f"unregistered readiness query: {command}")
            rc = 0 if self.active[unit] == "active" else 3
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
        if action in {"enable", "disable", "mask"}:
            unit = argv[-1]
            if unit not in self.enabled:
                pytest.fail(f"unregistered enable-state mutation: {command}")
            if action == "enable":
                self.enabled[unit] = "enabled-runtime" if "--runtime" in argv else "enabled"
            elif action == "mask":
                self.enabled[unit] = "masked-runtime" if "--runtime" in argv else "masked"
            else:
                self.enabled[unit] = "disabled"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if action == "stop":
            unit = argv[-1]
            if unit not in self.active:
                pytest.fail(f"unregistered stop: {command}")
            self.active[unit] = "inactive"
            if unit.startswith("orca_auto-runtime@"):
                self.active["orca_auto-engine-workers@alice.target"] = "inactive"
                self.active["orca_auto-queue-worker@alice.service"] = "inactive"
            elif unit.startswith("orca_auto-engine-workers@"):
                self.active["orca_auto-queue-worker@alice.service"] = "inactive"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if action == "restart":
            unit = argv[-1]
            if unit not in self.active:
                pytest.fail(f"unregistered restart: {command}")
            self.active[unit] = "active"
            if unit.startswith("orca_auto-runtime@"):
                self.active["orca_auto-engine-workers@alice.target"] = "active"
                self.active["orca_auto-queue-worker@alice.service"] = "active"
            elif unit.startswith("orca_auto-engine-workers@"):
                self.active["orca_auto-queue-worker@alice.service"] = "active"
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if action in {"daemon-reload", "reset-failed"}:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        pytest.fail(f"unsupported fake systemctl command: {command}")

    def __call__(
        self,
        argv: list[str],
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        normalized = list(argv)
        if normalized and normalized[0] == "sudo":
            normalized = normalized[1:]
        command = tuple(normalized)
        self.commands.append(command)
        injected = self.failures.get(command)
        if injected:
            rc = injected.pop(0)
            if rc != 0:
                return subprocess.CompletedProcess(normalized, rc, stdout="", stderr="injected")
        if normalized[0] == "systemctl":
            return self._systemctl(normalized)
        if normalized[0] == "mkdir":
            paths = normalized[2:] if normalized[1] == "-p" else normalized[1:]
            for raw_path in paths:
                path = self._validated_mutation_path(raw_path)
                try:
                    path.mkdir(parents=normalized[1] == "-p", exist_ok=normalized[1] == "-p")
                except FileExistsError:
                    return subprocess.CompletedProcess(normalized, 1, stdout="", stderr="exists")
            return subprocess.CompletedProcess(normalized, 0, stdout="", stderr="")
        if normalized[:3] == ["install", "-m", "0644"]:
            source = Path(normalized[3])
            destination = self._validated_mutation_path(normalized[4])
            destination.unlink(missing_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            destination.chmod(0o644)
            return subprocess.CompletedProcess(normalized, 0, stdout="", stderr="")
        pytest.fail(f"unsupported fake command: {command}")


def test_apply_systemd_install_plan_reports_direct_write_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _single_unit_plan(tmp_path)
    plan.unit_dir.write_text("not a directory", encoding="utf-8")

    assert cli_systemd_apply.apply_systemd_install_plan(plan) == 1
    assert "failed to write systemd units" in capsys.readouterr().err


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
        return subprocess.CompletedProcess(argv, 7 if argv[1] == "mkdir" else 0)

    result = cli_systemd_apply.apply_systemd_install_plan(
        _single_unit_plan(tmp_path, use_sudo=True),
        run=fake_run,
    )

    assert result == 7
    assert commands == [("sudo", "mkdir", "-p", str(tmp_path / "units"))]


def test_apply_systemd_install_plan_sudo_installs_units_then_runs_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "orca_auto.cli_systemd_apply.shutil.which",
        lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )
    plan = _single_unit_plan(
        tmp_path,
        use_sudo=True,
        commands=(
            ("systemctl", "daemon-reload"),
            ("systemctl", "restart", "orca_auto-runtime@alice.target"),
        ),
        enabled_unit="orca_auto-runtime@alice.target",
    )
    runner = _FakeSudoSystemd(plan.unit_dir)

    assert cli_systemd_apply.apply_systemd_install_plan(plan, run=runner) == 0

    destination = plan.units[0].destination
    assert destination.read_text(encoding="utf-8") == plan.units[0].content
    assert (destination.stat().st_mode & 0o777) == 0o644
    assert runner.commands == [
        ("mkdir", "-p", str(plan.unit_dir)),
        ("install", "-m", "0644", runner.commands[1][3], str(destination)),
        ("systemctl", "daemon-reload"),
        ("systemctl", "restart", "orca_auto-runtime@alice.target"),
    ]
    captured = capsys.readouterr().out
    assert f"installed: {destination}" in captured
    assert "enabled: orca_auto-runtime@alice.target" in captured


def test_apply_systemd_install_plan_keeps_new_units_when_command_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _single_unit_plan(
        tmp_path,
        commands=(
            ("systemctl", "daemon-reload"),
            ("systemctl", "restart", "orca_auto-runtime@alice.target"),
        ),
        enabled_unit="orca_auto-runtime@alice.target",
    )
    plan.unit_dir.mkdir()
    destination = plan.units[0].destination
    destination.write_text("[Unit]\nDescription=Old\n", encoding="utf-8")
    runner = _FakeSudoSystemd(
        plan.unit_dir,
        failures={("systemctl", "restart", "orca_auto-runtime@alice.target"): [5]},
    )

    assert cli_systemd_apply.apply_systemd_install_plan(plan, run=runner) == 5

    # There is deliberately no rollback: the new unit files stay in place and
    # the operator is told to rerun the installer after fixing the failure.
    assert destination.read_text(encoding="utf-8") == plan.units[0].content
    assert not destination.with_name(destination.name + ".tmp").exists()
    captured = capsys.readouterr()
    assert "rerun `orca_auto systemd install`" in captured.err
    assert "enabled:" not in captured.out


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


@pytest.mark.parametrize("unhealthy_label", ["runtime", "engines", "worker"])
@pytest.mark.parametrize("unhealthy_state", ["inactive", "not-found", "error: dbus down"])
def test_cmd_service_status_full_mode_rejects_any_non_active_required_unit(
    unhealthy_label: str,
    unhealthy_state: str,
) -> None:
    installed_units = (
        ("runtime", "orca_auto-runtime@alice.target", "enabled"),
        ("engines", "orca_auto-engine-workers@alice.target", "disabled"),
        ("worker", "orca_auto-queue-worker@alice.service", "disabled"),
        ("workflow", "orca_auto-workflow-worker@alice.service", "disabled"),
    )

    def _statuses(unhealthy: str | None) -> tuple[cli_systemd_status.ServiceUnitStatus, ...]:
        return tuple(
            cli_systemd_status.ServiceUnitStatus(
                label=label,
                unit=unit,
                active=unhealthy_state if label == unhealthy else "active",
                enabled=enabled,
            )
            for label, unit, enabled in installed_units
        )

    def _exit_code(statuses: tuple[cli_systemd_status.ServiceUnitStatus, ...]) -> int:
        return cli_systemd_status.cmd_service_status(
            Namespace(target_user="alice", json=True),
            deps=cli_systemd_status.ServiceCliDeps(
                collect_service_status=lambda target_user, run: statuses,
                run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
                which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            ),
        )

    # Baseline first: the healthy full-mode unit set must pass, so the rejection
    # below is caused by the unhealthy required unit rather than by a status
    # tuple that no longer matches the installed units.
    assert _exit_code(_statuses(None)) == 0
    assert _exit_code(_statuses(unhealthy_label)) == 1


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
