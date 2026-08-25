from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto import _process_evidence, cli_systemd_apply, cli_systemd_status, systemd_plan


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


def _make_fake_git_checkout(source_root: Path) -> None:
    (source_root / ".git").mkdir(parents=True)


def _fake_proc_stat(pid: int, *, start_ticks: int = 123_456) -> bytes:
    fields_after_comm = ["S", *("0" for _ in range(18)), str(start_ticks)]
    return f"{pid} (orca worker) {' '.join(fields_after_comm)}\n".encode()


def _states_run(
    states: dict[tuple[str, str], str],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Fake ``run`` answering ``systemctl <verb> <unit>`` from a state table."""

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

    return _fake_run


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


def _process_file_reader(
    import_sources: dict[int, Path], *, start_ticks: int = 123_456
) -> Callable[[str], bytes]:
    def _read(path: str) -> bytes:
        parts = Path(path).parts
        pid = int(parts[-2])
        if parts[-1] == "stat":
            return _fake_proc_stat(pid, start_ticks=start_ticks)
        assert parts[-1] == "environ"
        source = import_sources[pid]
        return f"{cli_systemd_status.PROCESS_IMPORT_SOURCE_ENV}={source}\0".encode()

    return _read


def test_module_cli_reexecs_once_with_actual_import_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.delenv(_process_evidence.PROCESS_IMPORT_SOURCE_ENV, raising=False)
    monkeypatch.setattr(
        _process_evidence.sys,
        "argv",
        ["orca_auto.cli", "queue", "worker", "--app", "orca"],
    )

    def _fake_execve(executable: str, argv: list[str], environment: dict[str, str]) -> None:
        captured.update(executable=executable, argv=argv, environment=environment)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(_process_evidence.os, "execve", _fake_execve)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        _process_evidence.exec_with_import_source_evidence()

    import_source = str(Path(_process_evidence.__file__).resolve(strict=False))
    assert captured["executable"] == sys.executable
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "orca_auto.cli",
        "queue",
        "worker",
        "--app",
        "orca",
    ]
    assert captured["environment"][_process_evidence.PROCESS_IMPORT_SOURCE_ENV] == import_source

    monkeypatch.setenv(_process_evidence.PROCESS_IMPORT_SOURCE_ENV, import_source)
    monkeypatch.setattr(
        _process_evidence.os,
        "execve",
        lambda *_args, **_kwargs: pytest.fail("matching evidence must not re-exec"),
    )
    _process_evidence.exec_with_import_source_evidence()


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


def test_systemd_renderer_escapes_literal_percent_only_in_rendered_paths(tmp_path: Path) -> None:
    repo, config_path = _make_repo(tmp_path / "checkout%i")
    unit_dir = tmp_path / "units"

    plan = systemd_plan.build_systemd_install_plan(
        target_user="alice",
        repo=repo,
        config=config_path,
        unit_dir=unit_dir,
        no_enable=True,
        is_root=lambda: True,
    )

    unit_by_name = {unit.name: unit for unit in plan.units}
    escaped_repo = str(repo.resolve(strict=False)).replace("%", "%%")
    escaped_config = str(config_path.resolve(strict=False)).replace("%", "%%")
    worker_content = unit_by_name["orca_auto-queue-worker@.service"].content
    assert f"WorkingDirectory={escaped_repo}" in worker_content
    assert f"Environment=ORCA_AUTO_CONFIG={escaped_config}" in worker_content
    assert f"ExecStart={escaped_repo}/.venv/bin/python" in worker_content
    assert f"ReadWritePaths={escaped_repo}/admission {escaped_repo}/orca_runs" in worker_content
    # Template-owned instance specifiers are not path data and must still expand.
    assert "User=%i" in worker_content
    assert "PartOf=orca_auto-engine-workers@%i.target" in worker_content
    assert (
        "Wants=orca_auto-queue-worker@%i.service"
        in unit_by_name["orca_auto-engine-workers@.target"].content
    )
    if shutil.which("systemd-analyze") is not None:
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


@pytest.mark.parametrize("character", ['"', "'", "\\", "$"])
@pytest.mark.parametrize("path_kind", ["repo", "config", "runtime"])
def test_systemd_rejects_unquoted_setting_metacharacters(
    tmp_path: Path,
    character: str,
    path_kind: str,
) -> None:
    repo_base = tmp_path / (f"checkout{character}value" if path_kind == "repo" else "checkout")
    repo, config_path = _make_repo(repo_base)
    if path_kind == "config":
        unsafe_config = config_path.with_name(f"orca{character}auto.yaml")
        config_path.rename(unsafe_config)
        config_path = unsafe_config
    elif path_kind == "runtime":
        config_path.write_text(
            "\n".join(
                [
                    f"runs_root: {repo / f'runs{character}root'}",
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

    with pytest.raises(
        ValueError,
        match="must not contain quotes, backslashes, or dollar signs",
    ):
        systemd_plan.build_systemd_install_plan(
            target_user="alice",
            repo=repo,
            config=config_path,
            unit_dir=tmp_path / "units",
            no_enable=True,
            is_root=lambda: True,
        )


def test_systemd_install_rejects_metacharacter_before_any_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, config_path = _make_repo(tmp_path)
    unsafe_config = config_path.with_name("orca$auto.yaml")
    config_path.rename(unsafe_config)
    unit_dir = tmp_path / "units"
    commands: list[tuple[str, ...]] = []

    def reject_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 99)

    result = cli_systemd_apply.cmd_systemd_install(
        Namespace(
            target_user="alice",
            repo=str(repo),
            config=str(unsafe_config),
            unit_dir=str(unit_dir),
            worker_only=False,
            no_enable=True,
            no_start=False,
            dry_run=False,
            no_sudo=True,
        ),
        deps=cli_systemd_apply.SystemdInstallCliDeps(
            run=reject_run,
            is_root=lambda: True,
        ),
    )

    assert result == 1
    assert commands == []
    assert not unit_dir.exists()
    assert "must not contain quotes, backslashes, or dollar signs" in capsys.readouterr().err


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

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            run=_states_run(states),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: None,
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
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: None,
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
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: None,
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

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None, json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            default_service_user=lambda: "alice",
            run=_states_run(states),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: None,
        ),
    )

    # A failed unit yields a non-zero exit even in JSON mode.
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_user"] == "alice"
    assert payload["ok"] is False
    worker = next(s for s in payload["services"] if s["label"] == "worker")
    assert worker["active"] == "failed"


def _healthy_worker_only_statuses() -> tuple[Any, ...]:
    return (
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
            enabled="enabled",
        ),
    )


def test_cmd_service_status_gates_on_stale_installed_metadata(capsys: Any) -> None:
    statuses = _healthy_worker_only_statuses()

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: ("0.1.0", "1.0.0"),
            collect_worker_staleness=lambda statuses, run=None: None,
        ),
    )

    # Every required unit is active, so the deployment is only unhealthy
    # because it reports a version it no longer runs.
    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["version_drift"] == {
        "installed": "0.1.0",
        "source": "1.0.0",
        "interpreter": sys.executable,
    }
    # The host runs one editable install per interpreter, so a verdict that did
    # not name its own is not actionable.
    assert sys.executable in captured.err
    assert "declares orca_auto 0.1.0" in captured.err
    assert "pip install -e ." in captured.err


def test_cmd_service_status_consults_the_real_detector_by_default(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every other status test injects the verdict, which leaves the production
    # wiring itself untested: unhooking the default would disable the gate with
    # a green suite.
    statuses = _healthy_worker_only_statuses()
    monkeypatch.setattr(cli_systemd_status, "installed_version_drift", lambda: ("0.1.0", "1.0.0"))

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            collect_worker_staleness=lambda statuses, run=None: None,
        ),
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out)["version_drift"]["installed"] == "0.1.0"


def test_cmd_service_status_reports_no_drift_for_a_current_install(capsys: Any) -> None:
    statuses = _healthy_worker_only_statuses()

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: None,
        ),
    )

    assert result == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["version_drift"] is None
    assert captured.err == ""


def test_cmd_service_status_gates_on_stale_worker_process(capsys: Any) -> None:
    statuses = _healthy_worker_only_statuses()
    verdict = {
        "head_commit_epoch": 1_000_000,
        "stale": [
            {
                "label": "worker",
                "unit": "orca_auto-queue-worker@alice.service",
                "pid": 4242,
                "started_epoch": 900_000,
            }
        ],
        "undetermined": [],
    }

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: verdict,
        ),
    )

    # Every required unit is active and the metadata is current, so the
    # deployment is only unhealthy because a worker still runs pre-deploy code.
    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["worker_staleness"] == verdict
    assert "orca_auto-queue-worker@alice.service (pid 4242)" in captured.err
    assert "pre-deploy code" in captured.err
    assert "orca_auto service restart" in captured.err


def test_cmd_service_status_gates_on_undetermined_worker_process(capsys: Any) -> None:
    statuses = _healthy_worker_only_statuses()
    verdict = {
        "head_commit_epoch": 1_000_000,
        "stale": [],
        "undetermined": [
            {
                "label": "worker",
                "unit": "orca_auto-queue-worker@alice.service",
                "detail": "no readable main PID",
            }
        ],
    }

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: verdict,
        ),
    )

    # A worker that cannot be inspected must not pass for fresh.
    assert result == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["worker_staleness"] == verdict
    assert "cannot judge worker code freshness" in captured.err
    assert "no readable main PID" in captured.err


def test_cmd_service_status_accepts_fresh_worker_processes(capsys: Any) -> None:
    statuses = _healthy_worker_only_statuses()
    verdict = {"head_commit_epoch": 1_000_000, "stale": [], "undetermined": []}

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: verdict,
        ),
    )

    assert result == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["worker_staleness"] == verdict
    assert captured.err == ""


def test_cmd_service_status_consults_the_real_staleness_collector_by_default(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every other status test injects the verdict, which leaves the production
    # wiring itself untested: unhooking the default would disable the gate with
    # a green suite.
    statuses = _healthy_worker_only_statuses()
    verdict = {
        "head_commit_epoch": 1_000_000,
        "stale": [
            {
                "label": "worker",
                "unit": "orca_auto-queue-worker@alice.service",
                "pid": 4242,
                "started_epoch": 900_000,
            }
        ],
        "undetermined": [],
    }
    monkeypatch.setattr(
        cli_systemd_status, "collect_worker_staleness", lambda statuses, run=None: verdict
    )

    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user="alice", json=True),
        deps=cli_systemd_status.ServiceCliDeps(
            collect_service_status=lambda target_user, run: statuses,
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            which=lambda name: "/bin/systemctl" if name == "systemctl" else None,
            installed_version_drift=lambda: None,
        ),
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out)["worker_staleness"] == verdict


def test_collect_worker_staleness_uses_head_update_not_old_commit_timestamp(
    tmp_path: Path,
) -> None:
    # The commit object was created a day before this checkout deployed it. The
    # stale worker started after the commit timestamp but before the checkout
    # update, which the former `%ct` comparison incorrectly reported as fresh.
    head_update_epoch = 1_785_747_750
    head_commit_epoch = head_update_epoch - 86_400
    head_sha = "a" * 40
    source_root = tmp_path / "checkout"
    _make_fake_git_checkout(source_root)
    start_stamps = {
        "orca_auto-queue-worker@alice.service": "Mon 2026-08-03 08:02:30 UTC",
        "orca_auto-workflow-worker@alice.service": "Mon 2026-08-03 10:02:30 UTC",
    }
    main_pids = {
        "orca_auto-queue-worker@alice.service": "41",
        "orca_auto-workflow-worker@alice.service": "42",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(source_root)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(source_root)
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_commit_epoch)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=ExecMainStartTimestamp", "--value"]:
            assert argv[4] == "--timestamp=utc"
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"{start_stamps[argv[5]]}\n", stderr=""
            )
        assert argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{main_pids[argv[4]]}\n", stderr="")

    statuses = (
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
            enabled="enabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="workflow",
            unit="orca_auto-workflow-worker@alice.service",
            active="active",
            enabled="disabled",
        ),
    )

    verdict = cli_systemd_status.collect_worker_staleness(
        statuses,
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert verdict["head_commit_epoch"] == head_commit_epoch
    assert verdict["head_update_epoch"] == head_update_epoch
    assert verdict["source_root"] == str(source_root)
    assert verdict["head_sha"] == head_sha
    assert verdict["undetermined"] == []
    # Only the pre-update worker service is stale; the fresh workflow worker and
    # the non-service engines target are not inspected as stale. In particular,
    # the stale worker started *after* the old commit object's timestamp.
    assert [entry["unit"] for entry in verdict["stale"]] == ["orca_auto-queue-worker@alice.service"]
    assert verdict["stale"][0]["pid"] == 41
    assert verdict["stale"][0]["started_epoch"] == head_update_epoch - 3600
    assert verdict["stale"][0]["started_epoch"] > head_commit_epoch
    assert verdict["stale"][0]["head_update_epoch"] == head_update_epoch
    assert {entry["source_root"] for entry in verdict["workers"]} == {str(source_root)}


def test_collect_worker_staleness_refreshes_shared_checkout_head_per_worker(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "checkout"
    _make_fake_git_checkout(source_root)
    old_sha = "a" * 40
    new_sha = "b" * 40
    old_update_epoch = 1_785_744_000
    worker_start_epoch = 1_785_747_600
    new_update_epoch = 1_785_751_200
    head_moved = False
    units = {
        "orca_auto-queue-worker@alice.service": "41",
        "orca_auto-workflow-worker@alice.service": "42",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal head_moved
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(source_root)]
            git_args = argv[3:]
            head_sha = new_sha if head_moved else old_sha
            update_epoch = new_update_epoch if head_moved else old_update_epoch
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(source_root)
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(update_epoch - 86_400)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=ExecMainStartTimestamp", "--value"]:
            assert argv[4] == "--timestamp=utc"
            if argv[5] == "orca_auto-workflow-worker@alice.service":
                head_moved = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Mon 2026-08-03 09:00:00 UTC\n",
                stderr="",
            )
        assert argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{units[argv[4]]}\n", stderr="")

    statuses = tuple(
        cli_systemd_status.ServiceUnitStatus(
            label=label,
            unit=unit,
            active="active",
            enabled="enabled",
        )
        for label, unit in (
            ("worker", "orca_auto-queue-worker@alice.service"),
            ("workflow", "orca_auto-workflow-worker@alice.service"),
        )
    )

    verdict = cli_systemd_status.collect_worker_staleness(
        statuses,
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert [entry["unit"] for entry in verdict["stale"]] == [
        "orca_auto-workflow-worker@alice.service"
    ]
    assert verdict["workers"][0]["head_sha"] == old_sha
    assert verdict["workers"][0]["started_epoch"] == worker_start_epoch
    assert verdict["workers"][1]["head_sha"] == new_sha
    assert verdict["workers"][1]["head_update_epoch"] == new_update_epoch


def test_collect_worker_staleness_observes_the_active_process_checkout(tmp_path: Path) -> None:
    unit_checkout = tmp_path / "unit-editable-checkout"
    import_source = unit_checkout / "src" / "orca_auto" / "_process_evidence.py"
    head_update_epoch = 1_785_747_750
    head_commit_epoch = head_update_epoch - 86_400
    head_sha = "b" * 40
    _make_fake_git_checkout(unit_checkout)
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# process evidence\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            # A regression to the status CLI's own editable checkout would not
            # match this process-observed root and fails the fixture immediately.
            assert argv[1:3] == ["-C", str(unit_checkout)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(unit_checkout)
            elif git_args == [
                "ls-files",
                "--error-unmatch",
                "--",
                "src/orca_auto/_process_evidence.py",
            ]:
                value = "src/orca_auto/_process_evidence.py"
            elif git_args == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/orca_auto",
            ]:
                value = ""
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_commit_epoch)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="77\n", stderr="")
        assert argv[:4] == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 08:02:30 UTC\n",
            stderr="",
        )

    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            label="worker",
            unit=unit,
            active="active",
            enabled="enabled",
        ),
    )
    observed_proc_paths: list[str] = []
    read_process_file = _process_file_reader({77: import_source})

    def _read_process_file(path: str) -> bytes:
        observed_proc_paths.append(path)
        return read_process_file(path)

    verdict = cli_systemd_status.collect_worker_staleness(
        statuses,
        run=_fake_run,
        read_process_file=_read_process_file,
    )

    assert verdict is not None
    assert "/proc/77/environ" in observed_proc_paths
    assert all(not path.endswith("/cwd") for path in observed_proc_paths)
    assert verdict["source_root"] == str(unit_checkout)
    assert verdict["head_sha"] == head_sha
    assert verdict["workers"][0]["import_source"] == str(import_source)
    assert [entry["unit"] for entry in verdict["stale"]] == [unit]


def test_collect_worker_staleness_refuses_dirty_import_package(tmp_path: Path) -> None:
    unit_checkout = tmp_path / "dirty-editable-checkout"
    import_source = unit_checkout / "src" / "orca_auto" / "_process_evidence.py"
    head_update_epoch = 1_785_744_000
    head_sha = "c" * 40
    _make_fake_git_checkout(unit_checkout)
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# process evidence\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(unit_checkout)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(unit_checkout)
            elif git_args == [
                "ls-files",
                "--error-unmatch",
                "--",
                "src/orca_auto/_process_evidence.py",
            ]:
                value = "src/orca_auto/_process_evidence.py"
            elif git_args == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/orca_auto",
            ]:
                value = " M src/orca_auto/cli.py"
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_update_epoch - 86_400)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="79\n", stderr="")
        assert argv[:4] == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 09:00:00 UTC\n",
            stderr="",
        )

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({79: import_source}),
    )

    assert verdict is not None
    assert verdict["workers"] == []
    assert verdict["stale"] == []
    assert verdict["undetermined"][0]["unit"] == unit
    assert "uncommitted source changes" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_returns_none_for_active_wheel_worker(tmp_path: Path) -> None:
    wheel_root = tmp_path / "wheel-runtime"
    import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            pytest.fail("a wheel worker has no checkout to inspect with git")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="91\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 10:02:30 UTC\n",
            stderr="",
        )

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({91: import_source}),
    )

    assert verdict is None


def test_collect_worker_staleness_treats_wheel_inside_git_cwd_as_uncompared(
    tmp_path: Path,
) -> None:
    git_cwd = tmp_path / "git-working-directory"
    _make_fake_git_checkout(git_cwd)
    import_source = (
        git_cwd
        / ".venv"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "orca_auto"
        / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(git_cwd)]
            if argv[3:] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, stdout=f"{git_cwd}\n", stderr="")
            assert argv[3:6] == ["ls-files", "--error-unmatch", "--"]
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="path is not tracked\n")
        assert argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]
        return subprocess.CompletedProcess(argv, 0, stdout="93\n", stderr="")

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({93: import_source}),
    )

    assert verdict is None


def test_collect_worker_staleness_fails_closed_when_process_identity_changes(
    tmp_path: Path,
) -> None:
    import_source = tmp_path / "wheel" / "site-packages" / "orca_auto" / "_process_evidence.py"
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    observed_ticks = iter((111, 222))
    unit = "orca_auto-queue-worker@alice.service"

    def _read_process_file(path: str) -> bytes:
        if path.endswith("/stat"):
            return _fake_proc_stat(94, start_ticks=next(observed_ticks))
        return f"{cli_systemd_status.PROCESS_IMPORT_SOURCE_ENV}={import_source}\0".encode()

    def _fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="94\n", stderr="")

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_read_process_file,
    )

    assert verdict is not None
    assert verdict["workers"] == []
    assert verdict["stale"] == []
    assert verdict["undetermined"][0]["unit"] == unit
    assert "process identity changed" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_does_not_require_start_time_for_wheel_worker(
    tmp_path: Path,
) -> None:
    wheel_root = tmp_path / "wheel-runtime"
    import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="91\n", stderr="")
        pytest.fail("a non-git worker has no checkout timestamp to compare")

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({91: import_source}),
    )

    assert verdict is None


def test_collect_worker_staleness_rechecks_pid_before_accepting_wheel_worker(
    tmp_path: Path,
) -> None:
    wheel_root = tmp_path / "wheel-runtime"
    import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    import_source.parent.mkdir(parents=True)
    import_source.write_text("# installed wheel\n", encoding="utf-8")
    unit = "orca_auto-queue-worker@alice.service"
    observed_pids = iter(("91", "92"))

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"{next(observed_pids)}\n", stderr=""
            )
        pytest.fail("a raced non-git worker must not be inspected as stable")

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({91: import_source}),
    )

    assert verdict is not None
    assert verdict["stale"] == []
    assert verdict["uncompared"] == []
    assert verdict["undetermined"][0]["unit"] == unit
    assert verdict["undetermined"][0]["detail"] == ("main PID changed during freshness inspection")


def test_collect_worker_staleness_skips_wheel_worker_in_mixed_deployment(
    tmp_path: Path,
) -> None:
    git_root = tmp_path / "git-worker"
    wheel_root = tmp_path / "wheel-worker"
    git_import_source = git_root / "src" / "orca_auto" / "_process_evidence.py"
    wheel_import_source = (
        wheel_root / "lib" / "python3.13" / "site-packages" / "orca_auto" / "_process_evidence.py"
    )
    _make_fake_git_checkout(git_root)
    git_import_source.parent.mkdir(parents=True)
    git_import_source.write_text("# editable source\n", encoding="utf-8")
    wheel_import_source.parent.mkdir(parents=True)
    wheel_import_source.write_text("# installed wheel\n", encoding="utf-8")
    head_update_epoch = 1_785_747_750
    head_commit_epoch = head_update_epoch - 86_400
    head_sha = "d" * 40
    git_unit = "orca_auto-queue-worker@alice.service"
    wheel_unit = "orca_auto-workflow-worker@alice.service"
    pids = {git_unit: "101", wheel_unit: "102"}
    starts = {
        git_unit: "Mon 2026-08-03 08:02:30 UTC",
        wheel_unit: "Mon 2026-08-03 10:02:30 UTC",
    }

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            assert argv[1:3] == ["-C", str(git_root)]
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(git_root)
            elif git_args == [
                "ls-files",
                "--error-unmatch",
                "--",
                "src/orca_auto/_process_evidence.py",
            ]:
                value = "src/orca_auto/_process_evidence.py"
            elif git_args == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/orca_auto",
            ]:
                value = ""
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            elif git_args == ["reflog", "-1", "--date=unix", "--format=%H%x00%gd"]:
                value = f"{head_sha}\0HEAD@{{{head_update_epoch}}}"
            else:
                assert git_args == ["show", "-s", "--format=%ct", head_sha]
                value = str(head_commit_epoch)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{pids[argv[4]]}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{starts[argv[5]]}\n", stderr="")

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=git_unit,
                active="active",
                enabled="enabled",
            ),
            cli_systemd_status.ServiceUnitStatus(
                label="workflow",
                unit=wheel_unit,
                active="active",
                enabled="disabled",
            ),
        ),
        run=_fake_run,
        read_process_file=_process_file_reader({101: git_import_source, 102: wheel_import_source}),
    )

    assert verdict is not None
    assert [entry["unit"] for entry in verdict["stale"]] == [git_unit]
    assert verdict["undetermined"] == []
    assert verdict["uncompared"] == [
        {
            "label": "workflow",
            "unit": wheel_unit,
            "pid": 102,
            "source_root": str(wheel_import_source.parent),
            "import_source": str(wheel_import_source),
            "reason": "installed_distribution",
        }
    ]


def test_collect_worker_staleness_fails_closed_without_checkout_update_evidence(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "checkout-without-reflog"
    (source_root / ".git").mkdir(parents=True)
    head_sha = "c" * 40
    unit = "orca_auto-queue-worker@alice.service"

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[0] == "git":
            git_args = argv[3:]
            if git_args == ["rev-parse", "--show-toplevel"]:
                value = str(source_root)
            elif git_args == ["rev-parse", "--verify", "HEAD^{commit}"]:
                value = head_sha
            else:
                assert git_args == [
                    "reflog",
                    "-1",
                    "--date=unix",
                    "--format=%H%x00%gd",
                ]
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="fatal: no reflog for HEAD\n",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=f"{value}\n", stderr="")
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="88\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Mon 2026-08-03 10:02:30 UTC\n",
            stderr="",
        )

    verdict = cli_systemd_status.collect_worker_staleness(
        (
            cli_systemd_status.ServiceUnitStatus(
                label="worker",
                unit=unit,
                active="active",
                enabled="enabled",
            ),
        ),
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert verdict["stale"] == []
    assert verdict["workers"] == []
    assert [entry["unit"] for entry in verdict["undetermined"]] == [unit]
    assert "cannot read checkout HEAD" in verdict["undetermined"][0]["detail"]
    assert "no reflog for HEAD" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_skips_inactive_workers_and_reports_unreadable_starts(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "checkout"
    (source_root / ".git").mkdir(parents=True)

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        if argv[:4] == ["systemctl", "show", "--property=MainPID", "--value"]:
            return subprocess.CompletedProcess(argv, 0, stdout="41\n", stderr="")
        # An empty ExecMainStartTimestamp is what systemd reports when it has
        # no start record to offer.
        assert argv[:4] == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
        ]
        return subprocess.CompletedProcess(argv, 0, stdout="\n", stderr="")

    statuses = (
        cli_systemd_status.ServiceUnitStatus(
            label="worker",
            unit="orca_auto-queue-worker@alice.service",
            active="active",
            enabled="enabled",
        ),
        cli_systemd_status.ServiceUnitStatus(
            label="workflow",
            unit="orca_auto-workflow-worker@alice.service",
            active="inactive",
            enabled="disabled",
        ),
    )

    verdict = cli_systemd_status.collect_worker_staleness(
        statuses,
        run=_fake_run,
        source_root=source_root,
    )

    # The inactive workflow worker is not a running process to judge, while the
    # active worker with no readable start record must surface instead of passing.
    assert verdict is not None
    assert verdict["stale"] == []
    assert [entry["unit"] for entry in verdict["undetermined"]] == [
        "orca_auto-queue-worker@alice.service"
    ]
    assert "cannot read unit start time" in verdict["undetermined"][0]["detail"]


def test_collect_worker_staleness_returns_none_outside_a_git_checkout(tmp_path: Path) -> None:
    source_root = tmp_path / "wheel-install"
    source_root.mkdir()

    verdict = cli_systemd_status.collect_worker_staleness(
        (),
        run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
        source_root=source_root,
    )

    assert verdict is None


def test_collect_worker_staleness_fails_closed_on_unreadable_history(tmp_path: Path) -> None:
    source_root = tmp_path / "checkout"
    (source_root / ".git").mkdir(parents=True)

    def _fake_run(
        argv: list[str],
        check: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, stdout, stderr, text
        assert argv[0] == "git"
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: bad revision\n")

    verdict = cli_systemd_status.collect_worker_staleness(
        (),
        run=_fake_run,
        source_root=source_root,
    )

    assert verdict is not None
    assert verdict["head_commit_epoch"] is None
    assert verdict["stale"] == []
    assert "cannot read checkout HEAD" in verdict["undetermined"][0]["detail"]
    assert "fatal: bad revision" in verdict["undetermined"][0]["detail"]


def test_cmd_service_status_fails_when_systemctl_is_missing(capsys: Any) -> None:
    result = cli_systemd_status.cmd_service_status(
        Namespace(target_user=None),
        deps=cli_systemd_status.ServiceCliDeps(which=lambda name: None),
    )

    assert result == 1
    assert "systemctl is not available" in capsys.readouterr().err


def test_cmd_service_restart_prefers_runtime_when_enabled(capsys: Any) -> None:
    commands: list[tuple[str, ...]] = []

    _fake_run = _recording_run(
        commands,
        {
            "is-active": (3, "inactive\n"),
            "is-enabled": (0, "enabled\n"),
        },
    )

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
    assert commands[-3:] == [
        ("systemctl", "reset-failed", "orca_auto-queue-worker@alice.service"),
        ("systemctl", "restart", "orca_auto-engine-workers@alice.target"),
        ("systemctl", "restart", "orca_auto-queue-worker@alice.service"),
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

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
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

    _fake_run = _recording_run(
        commands,
        {"is-active": (3, "inactive\n")},
        default=(0, "inactive\n"),
    )

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

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
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

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
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

    result = cli_systemd_status.cmd_service_restart(
        Namespace(target_user="alice"),
        deps=cli_systemd_status.ServiceCliDeps(
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


def test_default_service_user_resolves_the_account_behind_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under sudo, getpass reports root and every unit becomes an @root instance.

    systemd calls those a success -- `reset-failed` exits 0 on a unit it says is
    not loaded -- so the restart silently misses the real workers.
    """

    monkeypatch.setattr(cli_systemd_status, "_is_root", lambda: True)
    monkeypatch.setattr(cli_systemd_status.getpass, "getuser", lambda: "root")
    monkeypatch.setenv("SUDO_USER", "alice")

    assert cli_systemd_status._default_service_user() == "alice"


def test_default_service_user_keeps_root_for_a_real_root_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_systemd_status, "_is_root", lambda: True)
    monkeypatch.setattr(cli_systemd_status.getpass, "getuser", lambda: "root")
    monkeypatch.delenv("SUDO_USER", raising=False)

    assert cli_systemd_status._default_service_user() == "root"


def test_default_service_user_ignores_a_root_sudo_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_systemd_status, "_is_root", lambda: True)
    monkeypatch.setattr(cli_systemd_status.getpass, "getuser", lambda: "root")
    monkeypatch.setenv("SUDO_USER", "root")

    assert cli_systemd_status._default_service_user() == "root"


def test_default_service_user_ignores_sudo_user_without_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale SUDO_USER in a normal shell must not redirect the units."""

    monkeypatch.setattr(cli_systemd_status, "_is_root", lambda: False)
    monkeypatch.setattr(cli_systemd_status.getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SUDO_USER", "bob")

    assert cli_systemd_status._default_service_user() == "alice"


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
            installed_version_drift=lambda: None,
            collect_worker_staleness=lambda statuses, run=None: None,
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
                collect_worker_staleness=lambda statuses, run=None: None,
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
