from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_common
from orca_auto import cli_handlers as cli_run_dir
from orca_auto.cli import main as cli_main
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.flow.run_dir.layout import WorkflowRunDirLayout, inspect_workflow_run_dir
from orca_auto.orca.queue import adapter as queue_adapter


def test_cli_common_discovers_config_from_explicit_env_and_repo_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit_config = tmp_path / "explicit.yaml"
    explicit_config.write_text("workflow:\n  root: /tmp/workflows\n", encoding="utf-8")
    env_config = tmp_path / "env.yaml"
    env_config.write_text("workflow:\n  root: /tmp/workflows\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_config = repo_root / "config" / "orca_auto.yaml"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text("workflow:\n  root: /tmp/workflows\n", encoding="utf-8")

    assert cli_common._discover_shared_config_path(str(explicit_config)) == str(
        explicit_config.resolve()
    )

    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(env_config))
    assert cli_common._discover_shared_config_path(None) == str(env_config.resolve())

    monkeypatch.delenv(ORCA_AUTO_CONFIG_ENV_VAR)
    monkeypatch.setattr(cli_common, "_repo_root", lambda: repo_root)
    assert cli_common._discover_shared_config_path(None) == str(repo_config.resolve())
    assert cli_common._discover_workflow_root(str(tmp_path / "workflows")) == str(
        (tmp_path / "workflows").resolve()
    )
    assert cli_common._discover_workflow_root(" ") is None


def test_workflow_root_for_args_prefers_explicit_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_common,
        "shared_workflow_root_from_config",
        lambda config_path: (_ for _ in ()).throw(AssertionError("config should not be read")),
    )

    assert cli_common._workflow_root_for_args(
        argparse.Namespace(
            workflow_root="/tmp/explicit-workflows",
            orca_auto_config=None,
            config=None,
        )
    ) == str(Path("/tmp/explicit-workflows").resolve())


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

    def _fake_workflow_run_dir(args: Any) -> int:
        calls.append(("workflow", args.path))
        return 42

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)
    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _fake_workflow_run_dir)

    result = cli_run_dir.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 41
    assert calls == [("orca", str(target))]


def test_cmd_run_dir_dispatches_to_workflow_for_manifest_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "workflow_job"
    target.mkdir()
    (target / "flow.yaml").write_text("workflow_type: conformer_screening\n", encoding="utf-8")
    (target / "path.inp").write_text("$path\n$end\n", encoding="utf-8")
    calls: list[tuple[str, str, str | None]] = []

    def _fake_orca_run_dir(args: Any) -> int:
        calls.append(("orca", str(Path(args.path).resolve()), None))
        return 41

    def _fake_workflow_run_dir(args: Any) -> int:
        calls.append(
            (
                "workflow",
                str(Path(args.path).resolve()),
                str(Path(args.workflow_dir).resolve()),
            )
        )
        return 42

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)
    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _fake_workflow_run_dir)

    result = cli_run_dir.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 42
    assert calls == [("workflow", str(target), str(target))]


@pytest.mark.parametrize(
    ("marker_name", "marker_payload"),
    [
        ("job.inp", "! Opt\n"),
        ("flow.yaml", "workflow_type: conformer_screening\n"),
    ],
)
def test_cmd_run_dir_rejects_all_apps_inside_production_smoke_tree(
    marker_name: str,
    marker_payload: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    target = runs_root / SMOKE_RESULTS_DIRNAME / "batch-1" / "case-1" / "job"
    target.mkdir(parents=True)
    (target / marker_name).write_text(marker_payload, encoding="utf-8")
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    def _unexpected_submit(args: Any) -> int:
        raise AssertionError(f"reserved smoke target was dispatched: {args}")

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _unexpected_submit)
    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _unexpected_submit)

    assert (
        cli_run_dir.cmd_run_dir(
            SimpleNamespace(path=str(target), config=str(config), priority=None)
        )
        == 1
    )
    assert "reserved smoke-results subtree" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("marker_name", "marker_payload", "expected_app"),
    [
        ("job.inp", "! Opt\n", "orca"),
        ("flow.yaml", "workflow_type: conformer_screening\n", "workflow"),
    ],
)
def test_cmd_run_dir_allows_all_apps_with_case_local_runs_root(
    marker_name: str,
    marker_payload: str,
    expected_app: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    production_root = tmp_path / "runs"
    case_runs_root = (
        production_root / SMOKE_RESULTS_DIRNAME / "batch-1" / "case-1" / "runtime" / "runs"
    )
    target = case_runs_root / "job"
    target.mkdir(parents=True)
    (target / marker_name).write_text(marker_payload, encoding="utf-8")
    config = tmp_path / "case-orca_auto.yaml"
    config.write_text(f"runs_root: {case_runs_root}\n", encoding="utf-8")
    calls: list[str] = []

    def _record_submit(app: str) -> Callable[[Any], int]:
        def _submit(_args: Any) -> int:
            calls.append(app)
            return 0

        return _submit

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _record_submit("orca"))
    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _record_submit("workflow"))

    assert (
        cli_run_dir.cmd_run_dir(
            SimpleNamespace(path=str(target), config=str(config), priority=None)
        )
        == 0
    )
    assert calls == [expected_app]


def test_cmd_run_dir_rejects_symlink_aliases_across_reserved_tree_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    reserved_root = runs_root / SMOKE_RESULTS_DIRNAME
    outside_job = tmp_path / "outside-job"
    reserved_job = reserved_root / "reserved-job"
    outside_job.mkdir()
    reserved_job.mkdir(parents=True)
    (outside_job / "job.inp").write_text("! Opt\n", encoding="utf-8")
    (reserved_job / "job.inp").write_text("! Opt\n", encoding="utf-8")

    reserved_alias_to_outside = reserved_root / "alias-to-outside"
    reserved_alias_to_outside.symlink_to(outside_job, target_is_directory=True)
    outside_alias_to_reserved = tmp_path / "alias-to-reserved"
    outside_alias_to_reserved.symlink_to(reserved_job, target_is_directory=True)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    monkeypatch.setattr(
        cli_run_dir,
        "cmd_orca_run_dir",
        lambda args: pytest.fail(f"reserved alias was dispatched: {args}"),
    )

    for target in (reserved_alias_to_outside, outside_alias_to_reserved):
        assert (
            cli_run_dir.cmd_run_dir(
                SimpleNamespace(path=str(target), config=str(config), priority=None)
            )
            == 1
        )
        assert "reserved smoke-results subtree" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("marker_name", "marker_payload"),
    [
        ("job.inp", "! Opt\n"),
        ("flow.yaml", "workflow_type: conformer_screening\n"),
    ],
)
def test_cmd_run_dir_rejects_same_open_inode_moved_into_smoke_tree_after_gates(
    marker_name: str,
    marker_payload: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    reserved_job = runs_root / SMOKE_RESULTS_DIRNAME / "batch" / "normal-job"
    normal_job.mkdir(parents=True)
    reserved_job.parent.mkdir(parents=True)
    (normal_job / marker_name).write_text(marker_payload, encoding="utf-8")
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0

    def _move_open_inode_after_second_gate(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 2:
            normal_job.rename(reserved_job)

    def _unexpected_submit(args: Any) -> int:
        raise AssertionError(f"moved smoke target was dispatched: {args}")

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        _move_open_inode_after_second_gate,
    )
    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _unexpected_submit)
    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _unexpected_submit)

    assert (
        cli_run_dir.cmd_run_dir(
            SimpleNamespace(path=str(normal_job), config=str(config), priority=None)
        )
        == 1
    )
    # The first two checks passed; the publication check is the first one to
    # reject and therefore does not increment the counter after ``original_gate``.
    assert gate_count == 2
    error = capsys.readouterr().err
    assert "run-dir namespace target became unavailable" in error


def test_cli_run_dir_compensates_queue_if_inode_moves_after_precommit_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    reserved_job = runs_root / SMOKE_RESULTS_DIRNAME / "batch" / "normal-job"
    normal_job.mkdir(parents=True)
    reserved_job.parent.mkdir(parents=True)
    (normal_job / "job.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
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

    def _move_after_queue_precommit(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 5:
            normal_job.rename(reserved_job)

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        _move_after_queue_precommit,
    )

    assert cli_main(["run-dir", str(normal_job), "--config", str(config)]) == 1
    assert gate_count == 5
    assert reserved_job.is_dir()
    assert queue_adapter.list_queue(runs_root) == []
    assert not (runs_root / "job_locations.json").exists()
    assert not (reserved_job / "job_state.json").exists()
    assert not (reserved_job / ".orca_auto_orca_executions").exists()
    assert not (reserved_job / ".orca_auto_input_snapshots").exists()


def test_cli_run_dir_rechecks_before_first_orca_target_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    reserved_job = runs_root / SMOKE_RESULTS_DIRNAME / "batch" / "normal-job"
    normal_job.mkdir(parents=True)
    reserved_job.parent.mkdir(parents=True)
    input_payload = "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    (normal_job / "job.inp").write_text(input_payload, encoding="utf-8")
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

    def _move_after_central_dispatch(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 3:
            normal_job.rename(reserved_job)

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        _move_after_central_dispatch,
    )

    assert cli_main(["run-dir", str(normal_job), "--config", str(config)]) == 1
    assert gate_count == 3
    assert (reserved_job / "job.inp").read_text(encoding="utf-8") == input_payload
    assert queue_adapter.list_queue(runs_root) == []
    assert not (reserved_job / ".orca_auto_orca_executions").exists()
    assert not (reserved_job / ".orca_auto_input_snapshots").exists()


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


def test_cli_run_dir_rejects_workflow_namespace_replacement_before_payload_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    input_dir = workflow_root / "workflow_input"
    moved_input = workflow_root / "workflow_input_moved"
    input_dir.mkdir()
    flow_payload = "\n".join(
        (
            "workflow_type: conformer_screening",
            "max_crest_candidates: 1",
            "max_orca_stages: 1",
            "resources:",
            "  max_cores: 1",
            "  max_memory_gb: 1",
            "",
        )
    )
    original_xyz = "2\noriginal H2\nH 0 0 0\nH 0 0 0.74\n"
    replacement_xyz = "2\nreplacement H2\nH 0 0 0\nH 0 0 0.80\n"
    (input_dir / "flow.yaml").write_text(flow_payload, encoding="utf-8")
    (input_dir / "input.xyz").write_text(original_xyz, encoding="utf-8")

    fake_xtb = tmp_path / "fake-xtb"
    fake_crest = tmp_path / "fake-crest"
    for executable in (fake_xtb, fake_crest):
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        "\n".join(
            (
                f"runs_root: {workflow_root}",
                "scheduler:",
                "  max_active_simulations: 1",
                f"  admission_root: {tmp_path / 'admission'}",
                "workflow:",
                "  paths:",
                f"    xtb_executable: {fake_xtb}",
                f"    crest_executable: {fake_crest}",
                "resources:",
                "  max_cores_per_task: 1",
                "  max_memory_gb_per_task: 1",
                "",
            )
        ),
        encoding="utf-8",
    )
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0

    def replace_namespace_after_fourth_gate(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 4:
            input_dir.rename(moved_input)
            input_dir.mkdir()
            (input_dir / "flow.yaml").write_text(flow_payload, encoding="utf-8")
            (input_dir / "input.xyz").write_text(replacement_xyz, encoding="utf-8")

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        replace_namespace_after_fourth_gate,
    )

    assert cli_main(["run-dir", str(input_dir), "--config", str(config), "--json"]) == 1
    assert gate_count == 4
    assert (moved_input / "input.xyz").read_text(encoding="utf-8") == original_xyz
    assert (input_dir / "input.xyz").read_text(encoding="utf-8") == replacement_xyz
    assert not (workflow_root / "workflow_registry.json").exists()
    assert {path.name for path in workflow_root.iterdir()} <= {
        ".workflow_create.lock",
        "workflow_input",
        "workflow_input_moved",
    }


def test_cmd_run_dir_rejects_alias_retarget_after_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    reserved_job = runs_root / SMOKE_RESULTS_DIRNAME / "batch" / "reserved-job"
    normal_job.mkdir(parents=True)
    reserved_job.mkdir(parents=True)
    (normal_job / "job.inp").write_text("! Opt\n", encoding="utf-8")
    (reserved_job / "job.inp").write_text("! Opt\n", encoding="utf-8")
    alias = runs_root / "alias"
    alias.symlink_to(normal_job, target_is_directory=True)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    original_gate = cli_run_dir.validate_production_run_dir_target
    retargeted = False

    def _gate_then_retarget(path: str | Path, root: str | Path) -> None:
        nonlocal retargeted
        original_gate(path, root)
        if not retargeted:
            alias.unlink()
            alias.symlink_to(reserved_job, target_is_directory=True)
            retargeted = True

    observed: list[str] = []

    def _record_orca_dispatch(args: Any) -> int:
        observed.append(str(Path(args.path).resolve()))
        return 0

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        _gate_then_retarget,
    )
    monkeypatch.setattr(
        cli_run_dir,
        "cmd_orca_run_dir",
        _record_orca_dispatch,
    )

    assert (
        cli_run_dir.cmd_run_dir(SimpleNamespace(path=str(alias), config=str(config), priority=None))
        == 1
    )
    assert observed == []


def test_cmd_run_dir_rejects_replaced_directory_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    moved_job = runs_root / "normal-job-moved"
    reserved_job = runs_root / SMOKE_RESULTS_DIRNAME / "batch" / "reserved-job"
    normal_job.mkdir(parents=True)
    reserved_job.mkdir(parents=True)
    (normal_job / "job.inp").write_text("! Opt\n", encoding="utf-8")
    (reserved_job / "job.inp").write_text("! Opt\n", encoding="utf-8")
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0
    observed: list[tuple[str, str]] = []

    def _replace_after_second_gate(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 2:
            normal_job.rename(moved_job)
            normal_job.symlink_to(reserved_job, target_is_directory=True)

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        _replace_after_second_gate,
    )

    def _record_dispatch(args: Any) -> int:
        observed.append(
            (
                str(Path(args.path).resolve()),
                (Path(args.path) / "job.inp").read_text(encoding="utf-8"),
            )
        )
        return 0

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _record_dispatch)

    assert (
        cli_run_dir.cmd_run_dir(
            SimpleNamespace(path=str(normal_job), config=str(config), priority=None)
        )
        == 1
    )
    assert observed == []


def test_cmd_run_dir_rejects_real_smoke_dir_replacing_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    normal_job = runs_root / "normal-job"
    moved_job = runs_root / "normal-job-moved"
    reserved_job = runs_root / SMOKE_RESULTS_DIRNAME / "batch" / "reserved-job"
    normal_job.mkdir(parents=True)
    reserved_job.mkdir(parents=True)
    (normal_job / "job.inp").write_text("! Normal\n", encoding="utf-8")
    (reserved_job / "job.inp").write_text("! Smoke\n", encoding="utf-8")
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    original_gate = cli_run_dir.validate_production_run_dir_target
    gate_count = 0
    observed: list[tuple[str, str]] = []

    def _replace_after_second_gate(path: str | Path, root: str | Path) -> None:
        nonlocal gate_count
        original_gate(path, root)
        gate_count += 1
        if gate_count == 2:
            normal_job.rename(moved_job)
            reserved_job.rename(normal_job)

    def _record_dispatch(args: Any) -> int:
        observed.append(
            (
                str(Path(args.path).resolve()),
                (Path(args.path) / "job.inp").read_text(encoding="utf-8"),
            )
        )
        return 0

    monkeypatch.setattr(
        cli_run_dir,
        "validate_production_run_dir_target",
        _replace_after_second_gate,
    )
    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _record_dispatch)

    assert (
        cli_run_dir.cmd_run_dir(
            SimpleNamespace(path=str(normal_job), config=str(config), priority=None)
        )
        == 1
    )
    assert observed == []


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


def test_cmd_run_dir_allows_resource_overrides_for_workflow_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "workflow_job"
    target.mkdir()
    (target / "flow.yaml").write_text("workflow_type: conformer_screening\n", encoding="utf-8")
    calls: list[tuple[str, int | None, int | None]] = []

    def _fake_workflow_run_dir(args: Any) -> int:
        calls.append(
            ("workflow", getattr(args, "max_cores", None), getattr(args, "max_memory_gb", None))
        )
        return 42

    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _fake_workflow_run_dir)

    result = cli_run_dir.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
            max_cores=12,
            max_memory_gb=48,
            priority=None,
        )
    )

    assert result == 42
    assert calls == [("workflow", 12, 48)]


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

    def _fake_workflow_run_dir(args: Any) -> int:
        calls.append(("workflow", args.path))
        return 42

    monkeypatch.setattr(cli_run_dir, "cmd_orca_run_dir", _fake_orca_run_dir)
    monkeypatch.setattr(cli_run_dir, "cmd_workflow_run_dir", _fake_workflow_run_dir)

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
    assert "Could not infer run-dir target type from directory" in capsys.readouterr().err


def test_cmd_run_dir_requires_manifest_for_workflow_scaffold_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "workflow_scaffold"
    target.mkdir()
    (target / "input.xyz").write_text("3\nmol\nH 0 0 0\nH 0 0 0.7\nH 0 0 1.4\n", encoding="utf-8")

    result = cli_run_dir.cmd_run_dir(
        SimpleNamespace(
            path=str(target),
        )
    )

    assert result == 1
    assert "Could not infer run-dir target type from directory" in capsys.readouterr().err


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


def test_workflow_run_dir_layout_properties_and_manifest_detection(tmp_path: Path) -> None:
    ambiguous = WorkflowRunDirLayout(
        has_manifest=False,
        has_reaction_inputs=True,
        has_conformer_input=True,
    )
    reaction = WorkflowRunDirLayout(
        has_manifest=False,
        has_reaction_inputs=True,
        has_conformer_input=False,
    )
    conformer = WorkflowRunDirLayout(
        has_manifest=False,
        has_reaction_inputs=False,
        has_conformer_input=True,
    )

    assert ambiguous.is_ambiguous is True
    assert ambiguous.inferred_workflow_type is None
    assert reaction.inferred_workflow_type == "reaction_ts_search"
    assert conformer.inferred_workflow_type == "conformer_screening"

    target = tmp_path / "workflow"
    target.mkdir()
    (target / "reactant.xyz").write_text("1\nr\nH 0 0 0\n", encoding="utf-8")
    (target / "product.xyz").write_text("1\np\nH 0 0 1\n", encoding="utf-8")
    layout = inspect_workflow_run_dir(target)

    assert layout.has_reaction_inputs is True
    assert layout.inferred_workflow_type == "reaction_ts_search"
