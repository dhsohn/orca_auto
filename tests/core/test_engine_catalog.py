from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli as unified_cli
from orca_auto import cli_workers as cli_worker_specs
from orca_auto.core.engine_catalog import (
    activity_engine_entries,
    engine_catalog,
    engine_uses_workflow_stage_roots,
    known_engine_ids,
    supervised_engine_entries,
    workflow_stage_engine_entries,
)
from orca_auto.core.engines.registry import get_engine_definition
from orca_auto.core.indexing.roots import runtime_roots_for_cfg
from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    WORKFLOW_STAGE_DIRNAMES,
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths,
)
from orca_auto.core.queue.worker.admission import (
    engine_queue_worker_source,
    reserve_engine_queue_worker_slot,
)
from orca_auto.flow.activity import _cancel as activity_cancel
from orca_auto.flow.activity import _clear as activity_clear
from orca_auto.flow.activity import _collectors as activity_collectors
from orca_auto.flow.engines.xtb.job_inputs import SUPPORTED_JOB_TYPES


def _subparser(
    parser: argparse.ArgumentParser,
    *,
    dest: str,
    name: str,
) -> argparse.ArgumentParser:
    action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction) and action.dest == dest
    )
    return action.choices[name]


def _option_choices(parser: argparse.ArgumentParser, option: str) -> list[str]:
    action = parser._option_string_actions[option]
    assert action.choices is not None
    return [str(choice) for choice in action.choices]


def test_engine_catalog_is_import_safe() -> None:
    script = """
import sys
from orca_auto.core.engine_catalog import engine_catalog
assert all(entry.definition_module not in sys.modules for entry in engine_catalog())
assert not any(name.startswith(('orca_auto.flow', 'orca_auto.orca')) for name in sys.modules)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    subprocess.run([sys.executable, "-c", script], check=True, env=env)


@pytest.mark.parametrize(
    "module_name",
    [
        "orca_auto.core.engines.queue_worker",
        "orca_auto.core.engines.worker_child",
    ],
)
def test_engine_entrypoint_module_runs_without_eager_import_warning(module_name: str) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            module_name,
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "RuntimeWarning" not in result.stderr


def test_catalog_preserves_public_engine_and_routing_orders() -> None:
    assert known_engine_ids() == ("orca", "xtb", "crest")
    assert tuple(entry.engine_id for entry in supervised_engine_entries()) == (
        "orca",
        "crest",
        "xtb",
    )
    assert tuple(entry.engine_id for entry in workflow_stage_engine_entries()) == ("xtb", "crest")
    assert tuple(entry.engine_id for entry in activity_engine_entries()) == (
        "crest",
        "xtb",
        "orca",
    )
    assert tuple(WORKFLOW_STAGE_DIRNAMES) == ("crest", "xtb", "orca")
    entries = {entry.engine_id: entry for entry in engine_catalog()}
    assert entries["orca"].task_kinds == ("orca_run_inp",)
    assert entries["crest"].task_kinds == ("crest_conformer_search",)
    assert {kind.removeprefix("xtb_") for kind in entries["xtb"].task_kinds} == set(
        SUPPORTED_JOB_TYPES
    )


def test_every_catalog_engine_has_registry_supervision_admission_and_stage_metadata(
    tmp_path: Path,
) -> None:
    captured: list[dict[str, Any]] = []

    def reserve_slot(root: str, limit: int, **kwargs: Any) -> str:
        captured.append({"root": root, "limit": limit, **kwargs})
        return "slot-1"

    for entry in engine_catalog():
        captured.clear()
        assert get_engine_definition(entry.engine_id).engine == entry.engine_id
        assert cli_worker_specs._ENGINE_WORKER_MODULES[entry.engine_id] == entry.worker_module
        if entry.default_supervision_role == "default":
            assert entry.engine_id in cli_worker_specs._ENGINE_APPS
        else:
            assert entry.engine_id in cli_worker_specs._WORKFLOW_ENGINE_APPS

        assert engine_queue_worker_source(entry.engine_id) == entry.admission_source
        cfg = type(
            "Cfg",
            (),
            {
                "runtime": type(
                    "Runtime",
                    (),
                    {
                        "allowed_root": str(tmp_path),
                        "admission_root": str(tmp_path / ".admission"),
                        "admission_limit": 2,
                        "max_concurrent": 2,
                        "resolved_admission_root": str(tmp_path / ".admission"),
                        "resolved_admission_limit": 2,
                    },
                )()
            },
        )()

        assert (
            reserve_engine_queue_worker_slot(
                cfg,
                engine=entry.engine_id,
                reserve_slot_fn=reserve_slot,
            )
            == "slot-1"
        )
        assert captured[0]["source"] == entry.admission_source
        assert captured[0]["app_name"] == entry.app_id
        assert ("engine_process_state" in captured[0]) is entry.managed_admission
        assert captured[0].get("engine_launch_gated", False) is entry.engine_launch_gated

        assert WORKFLOW_STAGE_DIRNAMES[entry.engine_id] == entry.workflow_stage_dirname
        assert workflow_stage_dirnames_for_engine(entry.engine_id) == (
            entry.workflow_stage_dirname,
        )
        assert workflow_workspace_internal_engine_paths(
            tmp_path,
            engine=entry.engine_id,
        )["allowed_root"] == tmp_path / str(entry.workflow_stage_dirname)


def test_only_workflow_stage_engines_get_per_workflow_runtime_roots(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    workspace = workflow_root / "wf-all-engines"
    workspace.mkdir(parents=True)
    for entry in engine_catalog():
        (workspace / str(entry.workflow_stage_dirname)).mkdir()
    (workspace / WORKFLOW_FILE_NAME).write_text(
        json.dumps(
            {
                "workflow_id": "wf-all-engines",
                "stages": [
                    {"stage_id": f"{entry.engine_id}_01", "task": {"engine": entry.engine_id}}
                    for entry in engine_catalog()
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = type(
        "Cfg",
        (),
        {
            "runtime": type("Runtime", (), {"allowed_root": str(tmp_path / "runs")})(),
            "workflow_root": str(workflow_root),
        },
    )()

    assert engine_uses_workflow_stage_roots("demo") is False
    assert engine_uses_workflow_stage_roots("") is False
    for entry in engine_catalog():
        expects_stage_root = entry.workflow_stage_role == "workflow-stage"
        assert engine_uses_workflow_stage_roots(entry.engine_id) is expects_stage_root
        roots = runtime_roots_for_cfg(cfg, engine=entry.engine_id)
        stage_root = (workspace / str(entry.workflow_stage_dirname)).resolve()
        assert roots[0] == (tmp_path / "runs").resolve()
        assert (stage_root in roots) is expects_stage_root


def test_orca_worker_reservation_uses_catalog_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.orca.queue import worker as orca_worker

    captured: list[dict[str, Any]] = []

    def reserve_slot(root: Path, limit: int, **kwargs: Any) -> str:
        captured.append({"root": root, "limit": limit, **kwargs})
        return "slot-1"

    monkeypatch.setattr(orca_worker, "reserve_slot", reserve_slot)

    assert orca_worker._reserve_orca_worker_slot(tmp_path, 2) == "slot-1"
    orca_entry = next(entry for entry in engine_catalog() if entry.engine_id == "orca")
    assert captured[0]["source"] == orca_entry.admission_source
    assert captured[0]["app_name"] == orca_entry.app_id
    assert captured[0]["engine_launch_gated"] is True


def test_every_catalog_engine_has_queue_filter_list_cancel_and_clear_coverage() -> None:
    parser = unified_cli.build_parser()
    queue_parser = _subparser(parser, dest="command", name="queue")
    list_parser = _subparser(queue_parser, dest="queue_command", name="list")
    worker_parser = _subparser(queue_parser, dest="queue_command", name="worker")

    assert _option_choices(list_parser, "--engine") == [*known_engine_ids(), "workflow"]
    assert _option_choices(worker_parser, "--app") == [
        *(
            entry.engine_id
            for entry in supervised_engine_entries()
            if entry.default_supervision_role == "default"
        ),
        "workflow",
    ]

    provider_sources = {
        provider.source for provider in activity_collectors.activity_list_providers()
    }
    clear_counts = activity_clear._clear_counts()
    for entry in engine_catalog():
        assert entry.source_id in provider_sources
        assert f"{entry.engine_id}_queue_entries" in clear_counts
        if entry.workflow_stage_role == "workflow-stage":
            assert entry.engine_id in activity_cancel._CANCEL_ENGINE_TARGETS
        else:
            # Any other combination has no cancel handler and the dispatcher
            # fails closed with ValueError, so the catalog must not grow one.
            assert entry.activity_role == "orca-run"
            assert callable(activity_cancel.cancel_orca_target)
