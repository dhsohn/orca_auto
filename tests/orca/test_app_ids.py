from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli
from orca_auto.core.admission import admission_dir
from orca_auto.orca import app_ids


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


def test_app_ids_are_import_safe() -> None:
    script = """
import sys
from orca_auto.orca.app_ids import ORCA_ENGINE
assert ORCA_ENGINE == "orca"
allowed = {'orca_auto.orca', 'orca_auto.orca.app_ids'}
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


def test_app_ids_hold_the_persisted_orca_identity() -> None:
    assert app_ids.ORCA_ENGINE == "orca"
    assert app_ids.ORCA_AUTO_ORCA_APP_NAME == "orca_auto_orca"
    assert app_ids.ORCA_AUTO_ORCA_SOURCE == "orca_auto_orca"
    assert app_ids.ORCA_TASK_KIND == "orca_run_inp"
    # Persisted ``source`` label in admission_slots.json; not a module path.
    assert app_ids.ORCA_ADMISSION_SOURCE == "orca_auto.orca.queue_worker"
    assert app_ids.ORCA_ENGINE_LAUNCH_GATED is True


def test_orca_worker_reservation_uses_the_persisted_identity(
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
    admission_root = admission_dir(cfg.runtime.allowed_root)
    assert (
        orca_worker._try_reserve_admission_slot(admission_root, cfg.runtime.max_concurrent)
        == "slot-1"
    )
    assert captured[0]["source"] == "orca_auto.orca.queue_worker"
    assert captured[0]["app_name"] == "orca_auto_orca"
    assert captured[0]["engine_launch_gated"] is True
    assert captured[0]["engine_process_state"] == "idle"
    assert captured[0]["state"] == "reserved"
    assert captured[0]["root"] == admission_root
    assert captured[0]["limit"] == 2


def test_queue_list_and_worker_have_no_engine_selection_options() -> None:
    parser = cli.build_parser()
    queue_parser = _subparser(parser, dest="command", name="queue")
    list_parser = _subparser(queue_parser, dest="queue_command", name="list")
    worker_parser = _subparser(queue_parser, dest="queue_command", name="worker")
    assert "--engine" not in list_parser._option_string_actions
    assert "--kind" not in list_parser._option_string_actions
    assert "--status" in list_parser._option_string_actions
    assert "--app" not in worker_parser._option_string_actions
