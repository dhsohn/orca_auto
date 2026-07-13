from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from orca_auto.cli_common import (
    _configure_orca_logging,
    _engine_config_for_command,
)
from orca_auto.cli_errors import emit_error
from orca_auto.core.utils import normalize_text
from orca_auto.flow.run_dir.layout import inspect_workflow_run_dir


def cmd_init(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.init import cmd_init as _cmd_orca_init

    _configure_orca_logging(args)
    args.config = _engine_config_for_command(args)
    return int(_cmd_orca_init(args))


def cmd_orca_run_dir(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.run_inp import cmd_run_inp as _cmd_orca_run_dir

    _configure_orca_logging(args)
    args.config = _engine_config_for_command(args)
    return int(_cmd_orca_run_dir(args))


def cmd_xtb_md_run_dir(args: argparse.Namespace) -> int:
    from orca_auto.xtb_md.submission import submit_job_dir

    payload = submit_job_dir(
        job_dir=str(args.path),
        priority=int(args.priority),
        config_path=_engine_config_for_command(args),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    if str(payload.get("status") or "").lower() != "queued":
        if not bool(getattr(args, "json", False)):
            emit_error(payload.get("stderr") or payload.get("reason") or "xTB-MD submission failed")
        return 1
    if not bool(getattr(args, "json", False)):
        print(f"Queued xTB-MD job {payload['job_id']} ({payload['queue_id']})")
        print(f"job_dir: {payload['job_dir']}")
    return 0


def cmd_workflow_scaffold(args: argparse.Namespace) -> int:
    from orca_auto.flow.scaffold import cmd_scaffold as _cmd_workflow_scaffold

    return int(_cmd_workflow_scaffold(args))


def _detect_run_dir_app(args: argparse.Namespace) -> str:
    raw_path = normalize_text(getattr(args, "path", None))
    if not raw_path:
        raise ValueError("run-dir requires a target directory path")

    target = Path(raw_path).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"run-dir target not found: {target}")
    if not target.is_dir():
        raise ValueError(f"run-dir target is not a directory: {target}")

    workflow_layout = inspect_workflow_run_dir(target)
    markers = {
        "workflow": (target / "workflow.json").is_file() or workflow_layout.has_manifest,
        "orca": any(candidate.is_file() for candidate in target.glob("*.inp")),
        "xtb_md": (target / "xtb_md_job.yaml").is_file(),
    }
    detected = [app for app, present in markers.items() if present]
    if markers["xtb_md"] and len(detected) > 1:
        raise ValueError(
            "Ambiguous run-dir target: markers for multiple applications are present "
            f"({', '.join(detected)}). Keep exactly one of flow.yaml/workflow.json, "
            "*.inp, or xtb_md_job.yaml."
        )
    # Workflow inputs legitimately contain engine-specific ``*.inp`` files, so
    # a workflow manifest remains authoritative when no xTB-MD manifest exists.
    if markers["workflow"]:
        return "workflow"
    if markers["orca"]:
        return "orca"
    if markers["xtb_md"]:
        return "xtb_md"

    raise ValueError(
        "Could not infer run-dir target type from directory. "
        "Expected flow.yaml/workflow.json for a workflow, *.inp for ORCA, "
        "or xtb_md_job.yaml for standalone xTB-MD."
    )


def cmd_run_dir(args: Any) -> int:
    try:
        run_dir_app = _detect_run_dir_app(args)
    except ValueError as exc:
        emit_error(exc)
        return 1

    args.run_dir_app = run_dir_app
    if run_dir_app == "workflow":
        args.workflow_dir = args.path
        return int(cmd_workflow_run_dir(args))
    if run_dir_app == "xtb_md":
        if bool(getattr(args, "force", False)):
            emit_error("xTB-MD run-dir does not support --force, retry, or resume.")
            return 1
        if (
            getattr(args, "max_cores", None) is not None
            or getattr(args, "max_memory_gb", None) is not None
        ):
            emit_error(
                "xTB-MD run-dir does not support --max-cores or --max-memory-gb. "
                "Set smaller resource requests in xtb_md_job.yaml."
            )
            return 1
        if getattr(args, "priority", None) is None:
            args.priority = 10
        return int(cmd_xtb_md_run_dir(args))
    if (
        getattr(args, "max_cores", None) is not None
        or getattr(args, "max_memory_gb", None) is not None
    ):
        emit_error(
            "ORCA run-dir does not support --max-cores or --max-memory-gb. "
            "Edit %pal/%maxcore in the selected .inp, or use a workflow run-dir."
        )
        return 1
    if getattr(args, "priority", None) is None:
        args.priority = 10
    return int(cmd_orca_run_dir(args))


def cmd_workflow_run_dir(args: argparse.Namespace) -> int:
    from orca_auto.flow.cli.run_dir import cmd_run_dir as _cmd_workflow_run_dir

    shared_config = _engine_config_for_command(args)
    if shared_config:
        args.orca_auto_config = shared_config
    return int(_cmd_workflow_run_dir(args))


def cmd_orca_monitor(args: argparse.Namespace) -> int:
    from orca_auto.orca.commands.monitor import cmd_monitor as _cmd_orca_monitor

    _configure_orca_logging(args)
    args.config = _engine_config_for_command(args)
    return int(_cmd_orca_monitor(args))
