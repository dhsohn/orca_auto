from __future__ import annotations

import json
import shutil
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
from orca_auto.orca.commands import run_inp as run_inp_command
from orca_auto.orca.queue import adapter as queue_adapter
from tests.conftest import isolate_shared_config_discovery, make_queue_entry


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
        (None, "No such file or directory", "repair the reported state file"),
        ("runs_root: [unclosed\n", "Invalid YAML syntax", "repair the reported state file"),
        (
            "scheduler:\n  max_active_simulations: 4\n",
            "runs_root is missing or invalid in",
            "Set runs_root to an absolute directory path",
        ),
        ("runs_root: './runs'\n", "runs_root is missing or invalid in", "Set runs_root"),
        ("runs_root: '/mnt/c/runs'\n", "runs_root is missing or invalid in", "Set runs_root"),
        ("runs_root: {missing}\n", "runs_root does not exist: {missing}", "Check runs_root"),
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
        config_path.write_text(payload.format(missing=missing), encoding="utf-8")

    with pytest.raises(cli_handlers.CommandConfigError) as raised:
        cli_handlers.resolve_command_config(Namespace(config=str(config_path)))

    assert message.format(missing=missing) in str(raised.value)
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


def test_cmd_run_dir_dispatches_to_orca_for_inp_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def _fake_run_inp(args: Any, **_seams: Any) -> int:
        calls.append(("orca", str(Path(args.path).resolve())))
        return 41

    monkeypatch.setattr(cli_handlers, "_configure_orca_logging", lambda _args: None)
    monkeypatch.setattr(run_inp_command, "cmd_run_inp", _fake_run_inp)

    result = cli_handlers.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 41
    assert calls == [("orca", str(target))]


def test_cli_run_dir_rejects_orca_namespace_replacement_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    moved_original = runs_root / "moved-original"
    normal_job.mkdir(parents=True)
    original_payload = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    replacement_payload = "! SP\n* xyz 0 1\nHe 0 0 0\n*\n"
    (normal_job / "job.inp").write_text(original_payload, encoding="utf-8")
    fake_orca = tmp_path / "fake-orca"
    fake_orca.touch()
    fake_orca.chmod(0o755)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"runs_root: {runs_root}\norca:\n  paths:\n    orca_executable: {fake_orca}\n",
        encoding="utf-8",
    )
    original_gate = cli_handlers.validate_production_run_dir_target
    gate_count = 0

    def replace_after_mutation_preflight(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 4:
            normal_job.rename(moved_original)
            normal_job.mkdir()
            (normal_job / "job.inp").write_text(replacement_payload, encoding="utf-8")

    monkeypatch.setattr(
        cli_handlers,
        "validate_production_run_dir_target",
        replace_after_mutation_preflight,
    )

    assert cli_main(["run-dir", str(normal_job), "--config", str(config)]) == 1
    assert gate_count == 4
    assert queue_adapter.list_queue(runs_root) == []
    assert (moved_original / "job.inp").read_text(encoding="utf-8") == original_payload
    assert (normal_job / "job.inp").read_text(encoding="utf-8") == replacement_payload
    for job_dir in (moved_original, normal_job):
        assert not (job_dir / ".orca_auto_orca_executions").exists()
        assert not (job_dir / ".orca_auto_input_snapshots").exists()


def test_pinned_run_dir_does_not_relabel_downstream_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "orca-job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")

    def _raise_publication_error(_args: Any, **_seams: Any) -> int:
        raise OSError("disk full while publishing queue")

    monkeypatch.setattr(cli_handlers, "_configure_orca_logging", lambda _args: None)
    monkeypatch.setattr(run_inp_command, "cmd_run_inp", _raise_publication_error)

    with pytest.raises(OSError, match="disk full while publishing queue"):
        cli_handlers.cmd_run_dir(SimpleNamespace(path=str(target), priority=None))


def test_cmd_run_dir_prefers_orca_for_mixed_input_xyz_and_inp_without_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_job"
    target.mkdir()
    (target / "input.xyz").write_text("3\nmixed\nH 0 0 0\nH 0 0 0.7\nH 0 0 1.4\n", encoding="utf-8")
    (target / "tsopt.inp").write_text("! OptTS\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def _fake_run_inp(args: Any, **_seams: Any) -> int:
        calls.append(("orca", str(Path(args.path).resolve())))
        return 41

    monkeypatch.setattr(cli_handlers, "_configure_orca_logging", lambda _args: None)
    monkeypatch.setattr(run_inp_command, "cmd_run_inp", _fake_run_inp)

    result = cli_handlers.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 41
    assert calls == [("orca", str(target))]


def test_cmd_run_dir_reports_unknown_directory_layout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "unknown_job"
    target.mkdir()

    result = cli_handlers.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 1
    assert (
        "Could not infer run-dir target type: expected an ORCA *.inp file."
        in capsys.readouterr().err
    )


def test_cmd_run_dir_rejects_xyz_only_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "xyz_only"
    target.mkdir()
    (target / "input.xyz").write_text("3\nmol\nH 0 0 0\nH 0 0 0.7\nH 0 0 1.4\n", encoding="utf-8")

    result = cli_handlers.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 1
    assert (
        "Could not infer run-dir target type: expected an ORCA *.inp file."
        in capsys.readouterr().err
    )


def test_cmd_run_dir_reports_missing_and_file_targets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing"
    assert cli_handlers.cmd_run_dir(SimpleNamespace(path=str(missing))) == 1
    assert f"run-dir target not found: {missing.resolve()}" in capsys.readouterr().err

    file_target = tmp_path / "not-a-dir"
    file_target.write_text("not a directory\n", encoding="utf-8")
    assert cli_handlers.cmd_run_dir(SimpleNamespace(path=str(file_target))) == 1
    assert f"run-dir target is not a directory: {file_target.resolve()}" in capsys.readouterr().err


def _run_dir_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    runs_root = tmp_path / "runs"
    job = runs_root / "job"
    job.mkdir(parents=True)
    (job / "job.inp").write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    fake_orca = tmp_path / "fake-orca"
    fake_orca.touch()
    fake_orca.chmod(0o755)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"runs_root: {runs_root}\norca:\n  paths:\n    orca_executable: {fake_orca}\n",
        encoding="utf-8",
    )
    return runs_root, job, config


def test_cli_run_dir_reports_an_invalid_config_without_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root, job, config = _run_dir_fixture(tmp_path)
    config.write_text(config.read_text(encoding="utf-8") + "bogus_key: 1\n", encoding="utf-8")

    assert cli_main(["run-dir", str(job), "--config", str(config)]) == 1

    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "Unknown top-level config fields" in stderr
    assert not (runs_root / "queue.json").exists()


def test_cli_run_dir_reports_a_corrupt_queue_file_without_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root, job, config = _run_dir_fixture(tmp_path)
    queue_file = runs_root / "queue.json"
    queue_file.write_text("{not json", encoding="utf-8")

    assert cli_main(["run-dir", str(job), "--config", str(config)]) == 1

    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert f"Queue file is not valid JSON: {queue_file}" in stderr
    assert queue_file.read_text(encoding="utf-8") == "{not json"


def test_cli_run_dir_reports_a_failed_submission_on_stderr_even_with_a_log_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root, job, config = _run_dir_fixture(tmp_path)
    queue_file = runs_root / "queue.json"
    queue_file.write_text("{not json", encoding="utf-8")
    log_file = tmp_path / "submit.log"

    assert (
        cli_main(["run-dir", str(job), "--config", str(config), "--log-file", str(log_file)]) == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert f"Queue file is not valid JSON: {queue_file}" in captured.err
    # The submission log line stays; it is simply no longer the only trace.
    assert f"Queue file is not valid JSON: {queue_file}" in log_file.read_text(encoding="utf-8")

    assert (
        cli_main(
            [
                "run-dir",
                str(job),
                "--config",
                str(config),
                "--log-file",
                str(log_file),
                "--json",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert f"Queue file is not valid JSON: {queue_file}" in payload["error"]
    assert captured.err.startswith("error: ")


def test_cli_run_dir_json_success_carries_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root, job, config = _run_dir_fixture(tmp_path)
    from orca_auto.orca.commands import run_inp as run_inp_command

    def _fake_submit(args: Any) -> Any:
        entry = make_queue_entry(queue_id="q-ok", task_id="orca-ok", reaction_dir=job)
        worker = SimpleNamespace(status="inactive", pid=None, log_file=None, detail=None)
        return SimpleNamespace(
            status="submitted",
            stderr="",
            queued_result=SimpleNamespace(entry=entry, worker_info=worker),
            target=SimpleNamespace(reaction_dir=job),
        )

    monkeypatch.setattr(run_inp_command.submission, "submit_reaction_dir_to_queue", _fake_submit)

    assert cli_main(["run-dir", str(job), "--config", str(config), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["queue_id"] == "q-ok"
    assert payload["status"] == "queued"
