from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowWorkerOptionConfig:
    workflow_root_required: bool = False
    workflow_root_help: str = "Workflow root for workflow supervision"
    orca_auto_config_flags: tuple[str, ...] = ("--orca_auto-config", "--config")
    orca_auto_config_default: str | None = None
    no_submit_help: str = "Only sync/append workflow stages; do not submit newly actionable stages"
    include_once: bool = False
    refresh_registry_help: str = "Reindex the workflow registry before the first worker cycle"
    refresh_each_cycle_help: str = "Reindex the workflow registry before every worker cycle"
    max_cycles_default: int = 0
    max_cycles_help: str = "Workflow cycle limit"
    interval_seconds_default: float = 0.0
    interval_seconds_help: str = "Workflow worker sleep interval"
    lock_timeout_seconds_default: float = 0.0
    lock_timeout_seconds_help: str = "Workflow worker lock timeout"
    include_json: bool = True
    json_help: str = "Print JSON output"


DEFAULT_WORKFLOW_WORKER_OPTION_CONFIG = WorkflowWorkerOptionConfig()


def add_workflow_worker_cli_options(
    parser: argparse.ArgumentParser,
    *,
    config: WorkflowWorkerOptionConfig | None = None,
) -> None:
    config = config or DEFAULT_WORKFLOW_WORKER_OPTION_CONFIG
    parser.add_argument(
        "--workflow-root",
        required=config.workflow_root_required,
        help=config.workflow_root_help,
    )
    parser.add_argument(
        *config.orca_auto_config_flags,
        dest="orca_auto_config",
        default=config.orca_auto_config_default,
        help="Path to shared orca_auto.yaml",
    )
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help=config.no_submit_help,
    )
    if config.include_once:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run exactly one orchestration cycle.",
        )
    parser.add_argument(
        "--refresh-registry",
        action="store_true",
        help=config.refresh_registry_help,
    )
    parser.add_argument(
        "--refresh-each-cycle",
        action="store_true",
        help=config.refresh_each_cycle_help,
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=config.max_cycles_default,
        help=config.max_cycles_help,
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=config.interval_seconds_default,
        help=config.interval_seconds_help,
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=config.lock_timeout_seconds_default,
        help=config.lock_timeout_seconds_help,
    )
    if config.include_json:
        parser.add_argument("--json", action="store_true", help=config.json_help)


__all__ = [
    "DEFAULT_WORKFLOW_WORKER_OPTION_CONFIG",
    "WorkflowWorkerOptionConfig",
    "add_workflow_worker_cli_options",
]
