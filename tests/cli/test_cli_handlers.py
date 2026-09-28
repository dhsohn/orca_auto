from __future__ import annotations

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli_handlers
from orca_auto.activity import _cancel
from orca_auto.cli import main as cli_main
from orca_auto.core.config import files as config_files
from orca_auto.core.config.files import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.queue.persistence import entry_to_dict
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_APP_NAME
from tests.conftest import isolate_shared_config_discovery


def test_command_config_path_resolves_explicit_env_and_home_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit_config = tmp_path / "explicit.yaml"
    explicit_config.write_text("runs_root: /tmp/runs\n", encoding="utf-8")
    env_config = tmp_path / "env.yaml"
    env_config.write_text("runs_root: /tmp/runs\n", encoding="utf-8")
    home_config = Path.home() / "orca_auto" / "config" / "orca_auto.yaml"
    home_config.parent.mkdir(parents=True)
    home_config.write_text("runs_root: /tmp/runs\n", encoding="utf-8")

    assert cli_handlers.command_config_path(Namespace(config=str(explicit_config))) == str(
        explicit_config.resolve()
    )

    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(env_config))
    assert cli_handlers.command_config_path(Namespace(config=None)) == str(env_config.resolve())

    monkeypatch.delenv(ORCA_AUTO_CONFIG_ENV_VAR)
    assert cli_handlers.command_config_path(Namespace(config=None)) == str(home_config.resolve())


def test_discovery_seal_reaches_the_command_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Plant a config where the home fallback looks, so the test fails without
    # the seal on any machine, not only on one that has a live repository config.
    planted_home = tmp_path / "planted-home"
    planted = planted_home / "orca_auto" / "config" / "orca_auto.yaml"
    planted.parent.mkdir(parents=True)
    planted.write_text("runs_root: /tmp/planted-runs\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(planted_home))
    assert cli_handlers.command_config_path(Namespace(config=None)) == str(planted.resolve())

    isolate_shared_config_discovery(monkeypatch, tmp_path)

    with pytest.raises(cli_handlers.CommandConfigError, match="No orca_auto.yaml found"):
        cli_handlers.command_config_path(Namespace(config=None))
    explicit = tmp_path / "explicit.yaml"
    assert cli_handlers.command_config_path(Namespace(config=str(explicit))) == str(
        explicit.resolve()
    )


def test_resolve_command_config_reads_the_top_level_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    config = cli_handlers.resolve_command_config(Namespace(config=str(config_path)))

    assert config.path == str(config_path.resolve())
    assert config.runs_root == runs_root.resolve()


@pytest.mark.parametrize(
    ("payload", "message", "hint"),
    [
        (
            None,
            "Config file not found: {config}. Run `orca_auto init --config {config}`",
            "repair the reported state file",
        ),
        ("runs_root: [unclosed\n", "Invalid YAML syntax", "repair the reported state file"),
        (
            "scheduler:\n  max_active_simulations: 4\n",
            "runs_root is missing or invalid in",
            "Set runs_root to an absolute directory path",
        ),
        (
            "runs_root: './runs'\n",
            "runs_root is missing or invalid in {config}: "
            "runs_root must be an absolute Linux path.",
            "Set runs_root",
        ),
        (
            "runs_root: '/mnt/c/runs'\n",
            "runs_root is missing or invalid in {config}: "
            "runs_root must be a Linux path (Windows paths are not supported).",
            "Set runs_root",
        ),
        ("runs_root: {missing}\n", "runs_root does not exist: {missing}", "Check runs_root"),
        ("runs_root: {config}\n", "runs_root is not a directory: {config}", "Check runs_root"),
        ("runs_root: /tmp\nschedulr: {{}}\n", "Unknown top-level config fields", "repair"),
        ("runs_root: /tmp\nscheduler: []\n", "scheduler section must be a mapping", "repair"),
        (
            "runs_root: /tmp\nmessenger:\n  discord:\n    default_channel_id:\n",
            "messenger.discord.default_channel_id",
            "repair",
        ),
        (
            "runs_root: /tmp\nscheduler:\n  admission_root: /tmp/pool\n",
            "scheduler.admission_root was removed",
            "repair",
        ),
        (
            "runs_root: /tmp\norca:\n  scheduler:\n    max_active_simulations: 2\n",
            "Unknown orca config fields are not supported",
            "repair",
        ),
    ],
)
def test_resolve_command_config_names_each_unusable_config(
    tmp_path: Path, payload: str | None, message: str, hint: str
) -> None:
    config_path = tmp_path / "config.yaml"
    missing = tmp_path / "absent"
    if payload is not None:
        config_path.write_text(
            payload.format(missing=missing, config=config_path), encoding="utf-8"
        )

    with pytest.raises(cli_handlers.CommandConfigError) as raised:
        cli_handlers.resolve_command_config(Namespace(config=str(config_path)))

    assert message.format(missing=missing, config=config_path.resolve()) in str(raised.value)
    assert hint in raised.value.hint
    assert not missing.exists()


def test_each_command_loads_its_config_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_orca: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"runs_root: {runs_root}\norca:\n  paths:\n    orca_executable: {fake_orca}\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).resolve().parents[2] / "systemd", repo / "systemd")
    reads: list[Path] = []
    real_load_yaml_mapping = config_files.load_yaml_mapping

    def counting_load_yaml_mapping(path: Any, **kwargs: Any) -> Any:
        reads.append(Path(path))
        return real_load_yaml_mapping(path, **kwargs)

    monkeypatch.setattr(config_files, "load_yaml_mapping", counting_load_yaml_mapping)
    commands = {
        ("queue", "list", "--json"): 0,
        ("queue", "list", "clear", "--json"): 0,
        ("queue", "cancel", "no-such-job", "--json"): 1,
        ("index", "prune", "--json"): 0,
        ("index", "rebuild", "--dry-run", "--json"): 0,
        ("scratch", "list", "--json"): 1,
        ("scratch", "clear", "--all-stale", "--json"): 1,
        ("queue", "worker", "--json"): 0,
    }
    for argv, exit_code in commands.items():
        reads.clear()
        assert cli_main([*argv, "--config", str(config)]) == exit_code, argv
        assert reads == [config.resolve()], argv
    capsys.readouterr()

    reads.clear()
    install = [
        "systemd",
        "install",
        "--user",
        "alice",
        "--repo",
        str(repo),
        "--config",
        str(config),
        "--unit-dir",
        str(tmp_path / "units"),
        "--no-sudo",
        "--dry-run",
    ]
    assert cli_main(install) == 0
    assert reads == [config.resolve()]


@pytest.mark.parametrize("selected", ["home", "env", "explicit"])
def test_queue_list_and_cancel_use_the_same_discovered_config(
    selected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    isolate_shared_config_discovery(monkeypatch, tmp_path)
    configs = {
        "home": Path.home() / "orca_auto" / "config" / "orca_auto.yaml",
        "env": tmp_path / "env.yaml",
        "explicit": tmp_path / "explicit.yaml",
    }
    for name, config in configs.items():
        runs = tmp_path / f"{name}-runs"
        runs.mkdir()
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f"runs_root: {runs}\n", encoding="utf-8")
        entry = QueueEntry(
            queue_id=f"{name}-job",
            app_name=ORCA_AUTO_ORCA_APP_NAME,
            task_id=f"{name}-task",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.PENDING,
            priority=0,
            enqueued_at="2026-01-01T00:00:00Z",
            metadata={"reaction_dir": str(runs / "job")},
        )
        (runs / "queue.json").write_text(json.dumps([entry_to_dict(entry)]), encoding="utf-8")
    if selected in {"env", "explicit"}:
        monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(configs["env"]))
    options = ["--config", str(configs["explicit"])] if selected == "explicit" else []

    assert cli_main(["queue", "list", "--json", *options]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    target = listed["activities"][0]["activity_id"]
    assert target == f"{selected}-job"
    calls = []

    def cancel_stub(allowed_root: Path, queue_id: str, *, expected_entry: QueueEntry) -> QueueEntry:
        calls.append((allowed_root, queue_id))
        return replace(expected_entry, status=QueueStatus.CANCELLED)

    # Verify discovery and target selection without cancelling or signalling a job.
    monkeypatch.setattr(_cancel.queue_adapter, "cancel", cancel_stub)
    assert cli_main(["queue", "cancel", target, "--json", *options]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["activity_id"] == target
    assert calls == [((tmp_path / f"{selected}-runs").resolve(), target)]
