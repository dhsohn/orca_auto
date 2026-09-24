from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli as unified_cli
from orca_auto.activity import _cancel as activity_cancel
from orca_auto.orca.engine_catalog import (
    engine_catalog,
    find_engine_catalog_entry,
    get_engine_catalog_entry,
)


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


def test_engine_catalog_is_import_safe() -> None:
    script = """
import sys
from orca_auto.orca.engine_catalog import engine_catalog
assert tuple(entry.engine_id for entry in engine_catalog()) == ("orca",)
allowed = {'orca_auto.orca', 'orca_auto.orca.engine_catalog'}
assert not any(
    name.startswith(('orca_auto.flow', 'orca_auto.orca')) and name not in allowed
    for name in sys.modules
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    subprocess.run([sys.executable, "-c", script], check=True, env=env)


@pytest.mark.parametrize(
    "module_name",
    [
        "orca_auto.orca.commands.queue",
        "orca_auto.orca.commands.worker_child",
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


def test_catalog_holds_exactly_the_orca_identity() -> None:
    (entry,) = engine_catalog()
    assert entry.engine_id == "orca"
    assert entry.app_id == "orca_auto_orca"
    assert entry.source_id == "orca_auto_orca"
    assert entry.task_kinds == ("orca_run_inp",)
    # Persisted ``source`` label in admission_slots.json; not a module path.
    assert entry.admission_source == "orca_auto.orca.queue_worker"
    assert entry.engine_launch_gated is True


def test_catalog_lookup_normalizes_and_rejects_unknown_engines() -> None:
    assert find_engine_catalog_entry(" ORCA ") is engine_catalog()[0]
    assert find_engine_catalog_entry("other") is None
    assert get_engine_catalog_entry("orca") is engine_catalog()[0]
    with pytest.raises(ValueError, match=r"unsupported engine: other \(supported: orca\)"):
        get_engine_catalog_entry("other")
    with pytest.raises(ValueError, match="unsupported engine: <blank>"):
        get_engine_catalog_entry(None)


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

    from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig

    cfg = AppConfig(runtime=OrcaRuntimeConfig(allowed_root=str(tmp_path), max_concurrent=2))
    assert orca_worker._try_reserve_admission_slot(cfg) == "slot-1"
    orca_entry = next(entry for entry in engine_catalog() if entry.engine_id == "orca")
    assert captured[0]["source"] == orca_entry.admission_source
    assert captured[0]["app_name"] == orca_entry.app_id
    assert captured[0]["engine_launch_gated"] is True
    assert captured[0]["engine_process_state"] == "idle"
    assert captured[0]["state"] == "reserved"
    assert captured[0]["root"] == Path(cfg.runtime.resolved_admission_root)
    assert captured[0]["limit"] == cfg.runtime.resolved_admission_limit


def test_queue_list_and_worker_have_no_engine_selection_options() -> None:
    parser = unified_cli.build_parser()
    queue_parser = _subparser(parser, dest="command", name="queue")
    list_parser = _subparser(queue_parser, dest="queue_command", name="list")
    worker_parser = _subparser(queue_parser, dest="queue_command", name="worker")
    assert "--engine" not in list_parser._option_string_actions
    assert "--kind" not in list_parser._option_string_actions
    assert "--status" in list_parser._option_string_actions
    assert "--app" not in worker_parser._option_string_actions
    assert callable(activity_cancel.cancel_orca_target)
