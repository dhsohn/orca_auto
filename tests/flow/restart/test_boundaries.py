from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_handlers as cli_run_dir
from orca_auto.flow.cli import run_dir as flow_cli
from orca_auto.flow.restart import mutation as restart_mutation
from tests.flow.restart_helpers import _write_workflow


@pytest.mark.parametrize(
    "name",
    [
        "_active_restart_error",
        "_active_stage_rows",
        "_apply_flow_restart_settings",
        "_clear_phase_notification_state",
        "_reset_stage_for_restart",
        "_stage_needs_restart",
        "_stage_should_rematerialize",
    ],
)
def test_restart_mutation_does_not_forward_stage_operations(name: str) -> None:
    assert not hasattr(restart_mutation, name)


def test_flow_run_dir_reports_renamed_existing_workflow_without_restarting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "TS8_wf"
    _write_workflow(
        workspace,
        {
            "workflow_id": "TS8_original",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "stages": [],
            "metadata": {},
        },
    )
    workflow_before = (workspace / "workflow.json").read_bytes()

    rc = flow_cli.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workspace),
            workflow_root=str(root),
            force=False,
            json=False,
        )
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "does not match persisted workflow_id 'TS8_original'" in captured.err
    assert "Renaming an existing workflow directory is not supported" in captured.err
    assert (workspace / "workflow.json").read_bytes() == workflow_before
    assert not (root / "workflow_registry.json").exists()
    assert not (root / "workflow_registry.journal.jsonl").exists()


def test_flow_run_dir_reports_invalid_flow_yaml_during_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_invalid_manifest"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_invalid_manifest",
            "template_name": "conformer_screening",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_failed",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {},
                        "enqueue_payload": {},
                    },
                    "metadata": {},
                }
            ],
            "metadata": {},
        },
    )
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "# orca_auto workflow scaffold manifest",
                "workflow_type: conformer_screening",
                "crest_mode: nci",
                "# Optional CREST job overrides.",
                " crest:",
                "   gfn: ff",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = flow_cli.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workspace),
            workflow_root=None,
            force=False,
            json=False,
        )
    )

    assert rc == 1
    stderr = capsys.readouterr().err
    assert "Invalid Workflow manifest" in stderr
    assert "flow.yaml" in stderr
    assert "line 5, column 2" in stderr


def test_flow_run_dir_restarts_existing_workflow_workspace_without_flow_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_existing"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_existing",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_failed",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {"job_dir": "/tmp/crest"},
                        "enqueue_payload": {"job_dir": "/tmp/crest", "priority": 10},
                    },
                    "metadata": {"queue_id": "q_failed"},
                }
            ],
            "metadata": {},
        },
    )

    rc = flow_cli.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workspace),
            workflow_root=None,
            force=False,
            json=False,
        )
    )

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "workflow_id: wf_existing" in stdout
    assert "status: restarted" in stdout
    assert "restarted_count: 1" in stdout


def test_unified_run_dir_detects_existing_workflow_json_without_flow_yaml(tmp_path: Path) -> None:
    workspace = tmp_path / "wf_existing"
    _write_workflow(workspace, {"workflow_id": "wf_existing", "status": "failed", "stages": []})

    assert cli_run_dir._detect_run_dir_app(workspace) == "workflow"
