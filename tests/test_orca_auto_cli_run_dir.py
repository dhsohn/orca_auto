from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_handlers as cli_run_dir
from orca_auto.cli import main as cli_main
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config import discovery
from orca_auto.orca.queue import adapter as queue_adapter
from tests.config_discovery_helpers import isolate_shared_config_discovery
from tests.conftest import make_queue_entry


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_shared_config_discovery(monkeypatch, tmp_path)


def test_discovery_resolves_config_from_explicit_env_and_home_candidate(
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

    assert discovery.resolve_shared_config_path(str(explicit_config)) == str(
        explicit_config.resolve()
    )

    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(env_config))
    assert discovery.resolve_shared_config_path(None) == str(env_config.resolve())

    monkeypatch.delenv(ORCA_AUTO_CONFIG_ENV_VAR)
    assert discovery.resolve_shared_config_path(None) == str(home_config.resolve())


def test_cmd_run_dir_dispatches_to_orca_for_inp_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def _fake_orca_run_dir(args: Any) -> int:
        calls.append(("orca", str(Path(args.path).resolve())))
        return 41

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)

    result = cli_run_dir.cmd_run_dir(
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
    original_gate = cli_run_dir.validate_production_run_dir_target
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
        cli_run_dir,
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

    def _raise_publication_error(_args: Any) -> int:
        raise OSError("disk full while publishing queue")

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _raise_publication_error)

    with pytest.raises(OSError, match="disk full while publishing queue"):
        cli_run_dir.cmd_run_dir(SimpleNamespace(path=str(target), priority=None))


def test_cmd_run_dir_rejects_resource_overrides_for_orca_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")

    def _fake_orca_run_dir(args: Any) -> int:
        raise AssertionError(f"ORCA run-dir should not be called: {args}")

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)

    result = cli_run_dir.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
            max_cores=12,
            max_memory_gb=None,
            priority=None,
        )
    )

    assert result == 1
    err = capsys.readouterr().err
    assert "--max-cores" in err
    assert "--max-memory-gb" in err
    assert "%pal/%maxcore" in err


def test_cmd_run_dir_prefers_orca_for_mixed_input_xyz_and_inp_without_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "mixed_job"
    target.mkdir()
    (target / "input.xyz").write_text("3\nmixed\nH 0 0 0\nH 0 0 0.7\nH 0 0 1.4\n", encoding="utf-8")
    (target / "tsopt.inp").write_text("! OptTS\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def _fake_orca_run_dir(args: Any) -> int:
        calls.append(("orca", str(Path(args.path).resolve())))
        return 41

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)

    result = cli_run_dir.cmd_run_dir(
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

    result = cli_run_dir.cmd_run_dir(
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

    result = cli_run_dir.cmd_run_dir(
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
    assert cli_run_dir.cmd_run_dir(SimpleNamespace(path=str(missing))) == 1
    assert f"run-dir target not found: {missing.resolve()}" in capsys.readouterr().err

    file_target = tmp_path / "not-a-dir"
    file_target.write_text("not a directory\n", encoding="utf-8")
    assert cli_run_dir.cmd_run_dir(SimpleNamespace(path=str(file_target))) == 1
    assert f"run-dir target is not a directory: {file_target.resolve()}" in capsys.readouterr().err


def test_cmd_run_dir_sets_default_orca_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")
    seen: list[Any] = []

    def _fake_orca_run_dir(args: Any) -> int:
        seen.append(args)
        return 44

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)

    args = SimpleNamespace(path=str(target), priority=None)

    assert cli_run_dir.cmd_run_dir(args) == 44
    assert args.priority == 10
    assert seen == [args]


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
            context=SimpleNamespace(reaction_dir=job),
        )

    monkeypatch.setattr(run_inp_command.submission, "submit_reaction_dir_to_queue", _fake_submit)

    assert cli_main(["run-dir", str(job), "--config", str(config), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["queue_id"] == "q-ok"
    assert payload["status"] == "queued"
