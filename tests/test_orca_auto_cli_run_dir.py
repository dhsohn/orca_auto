from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_common
from orca_auto import cli_handlers as cli_run_dir
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.flow.run_dir.layout import WorkflowRunDirLayout, inspect_workflow_run_dir


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
        calls.append(("orca", args.path))
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
        calls.append(("orca", args.path, None))
        return 41

    def _fake_workflow_run_dir(args: Any) -> int:
        calls.append(("workflow", args.path, getattr(args, "workflow_dir", None)))
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


def test_cmd_run_dir_dispatches_to_xtb_md_for_exact_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "xtb_md_job"
    target.mkdir()
    (target / "xtb_md_job.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (target / "start.xyz").write_text("1\nH\nH 0 0 0\n", encoding="utf-8")
    calls: list[tuple[str, str, int]] = []

    def _fake_xtb_md_run_dir(args: Any) -> int:
        calls.append(("xtb_md", args.path, args.priority))
        return 43

    monkeypatch.setattr(cli_run_dir, "cmd_xtb_md_run_dir", _fake_xtb_md_run_dir)

    result = cli_run_dir.cmd_run_dir(SimpleNamespace(path=str(target), priority=None))

    assert result == 43
    assert calls == [("xtb_md", str(target), 10)]


@pytest.mark.parametrize(
    ("extra_name", "extra_payload"),
    [
        ("flow.yaml", "workflow_type: conformer_screening\n"),
        ("job.inp", "! Opt\n"),
    ],
)
def test_cmd_run_dir_rejects_ambiguous_xtb_md_markers(
    extra_name: str,
    extra_payload: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "ambiguous_job"
    target.mkdir()
    (target / "xtb_md_job.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (target / extra_name).write_text(extra_payload, encoding="utf-8")

    assert cli_run_dir.cmd_run_dir(SimpleNamespace(path=str(target))) == 1
    error = capsys.readouterr().err
    assert "Ambiguous run-dir target" in error
    assert "xtb_md" in error


@pytest.mark.parametrize(
    "overrides",
    [
        {"force": True},
        {"max_cores": 2},
        {"max_memory_gb": 4},
    ],
)
def test_cmd_run_dir_rejects_xtb_md_retry_and_cli_resource_overrides(
    overrides: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "xtb_md_job"
    target.mkdir()
    (target / "xtb_md_job.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    monkeypatch.setattr(
        cli_run_dir,
        "cmd_xtb_md_run_dir",
        lambda args: (_ for _ in ()).throw(AssertionError(f"unexpected submit: {args}")),
    )
    values = {
        "path": str(target),
        "priority": None,
        "force": False,
        "max_cores": None,
        "max_memory_gb": None,
    }
    values.update(overrides)
    args = SimpleNamespace(**values)

    assert cli_run_dir.cmd_run_dir(args) == 1
    error = capsys.readouterr().err
    if overrides.get("force"):
        assert "retry, or resume" in error
    else:
        assert "xtb_md_job.yaml" in error


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
        calls.append(("orca", args.path))
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
