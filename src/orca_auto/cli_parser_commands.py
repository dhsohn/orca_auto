from __future__ import annotations

import argparse

from orca_auto import cli_handlers
from orca_auto.flow.templates import WORKFLOW_SCAFFOLD_SHORTCUTS

from .cli_parser_common import (
    add_engine_config_argument,
    add_json_argument,
    add_orca_logging_arguments,
    add_resource_override_arguments,
)


def _add_workflow_scaffold_shortcut(
    scaffold_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    name: str,
    workflow_type: str,
    help_text: str,
) -> None:
    parser = scaffold_subparsers.add_parser(name, help=help_text)
    parser.add_argument("root", help="Workflow input directory to create")
    parser.set_defaults(
        func=cli_handlers.cmd_workflow_scaffold,
        workflow_type=workflow_type,
    )


def add_run_dir_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    run_dir_parser = subparsers.add_parser(
        "run-dir",
        help="Submit an ORCA or workflow input directory.",
    )
    add_engine_config_argument(run_dir_parser)
    add_orca_logging_arguments(run_dir_parser)
    run_dir_parser.add_argument("path", help="ORCA or workflow input directory")
    run_dir_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force ORCA re-run, or allow restarting an existing workflow workspace outside "
            "failed status"
        ),
    )
    run_dir_parser.add_argument(
        "--priority",
        type=int,
        default=None,
        help="Queue priority when submission is enqueued (lower = higher)",
    )
    add_resource_override_arguments(run_dir_parser)
    add_json_argument(run_dir_parser, help_text="Print JSON submission output")
    run_dir_parser.set_defaults(func=cli_handlers.cmd_run_dir)


def add_init_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    init_parser = subparsers.add_parser(
        "init",
        help="Interactively create or update the shared orca_auto.yaml config.",
    )
    add_engine_config_argument(init_parser)
    add_orca_logging_arguments(init_parser)
    init_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing config without confirmation"
    )
    init_parser.set_defaults(func=cli_handlers.cmd_init)


def add_scaffold_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    scaffold_parser = subparsers.add_parser(
        "scaffold",
        help="Create raw input workflow scaffold directories.",
    )
    scaffold_subparsers = scaffold_parser.add_subparsers(dest="scaffold_app", required=True)

    for name, workflow_type, help_text in WORKFLOW_SCAFFOLD_SHORTCUTS:
        _add_workflow_scaffold_shortcut(
            scaffold_subparsers,
            name=name,
            workflow_type=workflow_type,
            help_text=help_text,
        )


def add_monitor_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # ``scan-notify`` performs a single filesystem scan and sends messenger
    # alerts, then exits.
    monitor_parser = subparsers.add_parser(
        "scan-notify",
        help=(
            "Run a one-shot scan for newly discovered DFT results (or scan "
            "failures) and send ORCA messenger alerts. Not a live monitor."
        ),
    )
    add_engine_config_argument(monitor_parser)
    add_orca_logging_arguments(monitor_parser)
    monitor_parser.set_defaults(func=cli_handlers.cmd_orca_monitor)


def add_smoke_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        choices=("fake", "real-orca", "all"),
        default="fake",
        help="Smoke profile to run (default: fake)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Run one scenario id; may be passed more than once",
    )
    parser.add_argument(
        "--runs-root",
        default="",
        help="Override runs_root; real profiles require it to match the shared config",
    )
    parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="config",
        default="",
        help="Path to shared orca_auto.yaml (auto-discovered by default)",
    )
    parser.add_argument("--repo-root", default="", help=argparse.SUPPRESS)


def add_smoke_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    smoke_parser = subparsers.add_parser(
        "smoke",
        help="Run retained developer smoke tests and build a review packet.",
        description=(
            "Run retained ORCA/xTB/workflow smoke scenarios from the source checkout "
            "backing this orca_auto command. The default fake profile uses the shared "
            "config's runs_root."
        ),
    )
    add_smoke_arguments(smoke_parser)
    smoke_parser.set_defaults(func=cli_handlers.cmd_smoke)


__all__ = [
    "add_init_parser",
    "add_monitor_parser",
    "add_run_dir_parser",
    "add_scaffold_parser",
    "add_smoke_arguments",
    "add_smoke_parser",
]
