from __future__ import annotations

import argparse

from orca_auto import cli_queue, cli_workers
from orca_auto.flow.cli.worker_options import (
    WorkflowWorkerOptionConfig,
    add_workflow_worker_cli_options,
)

from .cli_parser_common import add_json_argument


def _add_queue_list_parser(
    queue_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    list_parser = queue_subparsers.add_parser(
        "list", help="List workflows and engine activities together."
    )
    list_parser.add_argument(
        "action",
        nargs="?",
        choices=["clear"],
        help="Remove completed/failed/cancelled entries from the unified activity list",
    )
    list_parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        help="Path to shared orca_auto.yaml",
    )
    list_parser.add_argument(
        "--limit", type=int, default=0, help="Optional maximum number of activities to print"
    )
    list_parser.add_argument(
        "--refresh", action="store_true", help="Refresh workflow registry before listing"
    )
    list_parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously refresh the list until interrupted (Ctrl-C)",
    )
    list_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Refresh interval in seconds for --watch (default 2.0)",
    )
    list_parser.add_argument(
        "--engine",
        action="append",
        choices=["orca", "xtb", "crest", "workflow"],
        help="Filter by engine; may be passed more than once",
    )
    list_parser.add_argument(
        "--status", action="append", help="Filter by status; may be passed more than once"
    )
    list_parser.add_argument(
        "--kind",
        action="append",
        choices=["job", "workflow"],
        help="Filter by activity kind; may be passed more than once",
    )
    add_json_argument(list_parser)
    list_parser.set_defaults(func=cli_queue.cmd_queue_list)


def _add_queue_cancel_parser(
    queue_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    cancel_parser = queue_subparsers.add_parser(
        "cancel", help="Cancel a workflow or engine activity."
    )
    cancel_parser.add_argument(
        "target", help="Activity id, workflow id, queue id, run id, or known path alias"
    )
    cancel_parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        help="Path to shared orca_auto.yaml",
    )
    add_json_argument(cancel_parser)
    cancel_parser.set_defaults(func=cli_queue.cmd_queue_cancel)


def _add_queue_worker_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app",
        action="append",
        choices=["orca", "workflow"],
        help="Worker app to supervise; may be passed more than once",
    )
    add_workflow_worker_cli_options(
        parser,
        config=WorkflowWorkerOptionConfig(
            json_help="Print worker commands as JSON without starting them"
        ),
    )


def _add_queue_worker_parser(
    queue_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    worker_parser = queue_subparsers.add_parser("worker", help="Run the unified worker supervisor.")
    _add_queue_worker_options(worker_parser)
    worker_parser.set_defaults(func=cli_workers.cmd_queue_worker)


def add_queue_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    queue_parser = subparsers.add_parser(
        "queue",
        help="Unified queue and worker commands across ORCA, workflow-managed internal engines, and workflows.",
    )
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command", required=True)
    _add_queue_list_parser(queue_subparsers)
    _add_queue_cancel_parser(queue_subparsers)
    _add_queue_worker_parser(queue_subparsers)


__all__ = ["add_queue_parser"]
