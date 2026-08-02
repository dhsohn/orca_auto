"""Build the ``orca_auto`` argument parser.

``argparse`` writes its own ``prog: error: ...`` line straight to ``stderr``,
bypassing :mod:`orca_auto.cli_errors`. :class:`OrcaAutoArgumentParser` funnels
those errors through :func:`orca_auto.cli_errors.emit_error` so every
user-facing failure — runtime or argument-parsing — shares one
``error:``/``hint:`` format, and adds a "did you mean" suggestion when an
unknown subcommand looks like a typo. ``add_subparsers`` defaults
``parser_class`` to ``type(self)``, so every nested subparser inherits this
behavior automatically once the top parser uses it.
"""

from __future__ import annotations

import argparse
import difflib
import re
from typing import NoReturn, cast

from orca_auto import cli_handlers, cli_queue, cli_workers
from orca_auto._version import package_version
from orca_auto.cli_errors import emit_error
from orca_auto.cli_systemd_apply import cmd_systemd_install
from orca_auto.cli_systemd_status import cmd_service_restart, cmd_service_status
from orca_auto.core.engine_catalog import known_engine_ids, supervised_engine_entries
from orca_auto.flow.cli.worker_options import (
    WorkflowWorkerOptionConfig,
    add_workflow_worker_cli_options,
)
from orca_auto.flow.templates import WORKFLOW_SCAFFOLD_SHORTCUTS
from orca_auto.systemd_plan import DEFAULT_SYSTEMD_UNIT_DIR

# Matches argparse's stock invalid-choice message. Older Python quotes each
# choice (``choose from 'queue', 'run-dir'``); 3.12+ drops the quotes
# (``choose from queue, run-dir``), so both forms are handled below.
_INVALID_CHOICE_RE = re.compile(
    r"invalid choice: '(?P<value>[^']*)' \(choose from (?P<choices>.+)\)"
)


def _suggestion_hint(message: str) -> str | None:
    """Return a "did you mean ...?" hint for an invalid-choice ``message``."""

    match = _INVALID_CHOICE_RE.search(message)
    if not match:
        return None
    choices = [part.strip().strip("'") for part in match.group("choices").split(",")]
    choices = [choice for choice in choices if choice]
    if not choices:
        return None
    close = difflib.get_close_matches(match.group("value"), choices, n=1, cutoff=0.5)
    if close:
        return f"did you mean `{close[0]}`?"
    return f"valid choices: {', '.join(choices)}"


class OrcaAutoArgumentParser(argparse.ArgumentParser):
    """``ArgumentParser`` whose errors use the shared ``error:`` styling."""

    def error(self, message: str) -> NoReturn:
        hint = _suggestion_hint(message)
        if hint is None:
            hint = f"run `{self.prog} --help` for usage."
        emit_error(message, hint=hint)
        self.exit(2)


def add_engine_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="config",
        default=None,
        help="Path to shared orca_auto.yaml",
    )


def add_orca_logging_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--log-file", default=None, help="Write logs to file (with rotation, max 10MB x 5)"
    )


def add_json_argument(
    parser: argparse.ArgumentParser, *, help_text: str = "Print JSON output"
) -> None:
    parser.add_argument("--json", action="store_true", help=help_text)


def add_resource_override_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-cores",
        type=int,
        default=None,
        help=(
            "Set workflow max cores; standalone ORCA uses %%pal/%%maxcore from the selected input"
        ),
    )
    parser.add_argument(
        "--max-memory-gb",
        type=int,
        default=None,
        help=(
            "Set workflow max memory in GB; standalone ORCA uses %%pal/%%maxcore "
            "from the selected input"
        ),
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
        "--engine",
        action="append",
        choices=[*known_engine_ids(), "workflow"],
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
        choices=[
            *(
                entry.engine_id
                for entry in supervised_engine_entries()
                if entry.default_supervision_role == "default"
            ),
            "workflow",
        ],
        help=(
            "Worker app to supervise; defaults to ORCA only and may be passed more than once; "
            "workflow explicitly includes its xTB/CREST workers"
        ),
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


def add_systemd_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    systemd_parser = subparsers.add_parser(
        "systemd",
        help="Install orca_auto systemd runtime units.",
    )
    systemd_subparsers = systemd_parser.add_subparsers(dest="systemd_command", required=True)

    install_parser = systemd_subparsers.add_parser(
        "install",
        help="Render, install, reload, and optionally enable orca_auto systemd units.",
    )
    install_parser.add_argument(
        "--user",
        dest="target_user",
        required=True,
        help="Linux user name used for the templated systemd instance",
    )
    install_parser.add_argument(
        "--repo",
        required=True,
        help="Absolute path to the orca_auto repository checkout",
    )
    install_parser.add_argument(
        "--config",
        default=None,
        help="config path rendered into the units when it differs from the default",
    )
    install_parser.add_argument(
        "--unit-dir",
        default=str(DEFAULT_SYSTEMD_UNIT_DIR),
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--worker-only",
        action="store_true",
        help="enable only the engine-worker target instead of the full runtime",
    )
    install_parser.add_argument(
        "--no-enable",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--no-start",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--no-sudo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    install_parser.set_defaults(func=cmd_systemd_install)


def add_service_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    service_parser = subparsers.add_parser(
        "service",
        help="Check or restart orca_auto systemd services.",
    )
    service_subparsers = service_parser.add_subparsers(dest="service_command", required=True)

    status_parser = service_subparsers.add_parser(
        "status",
        help="Show orca_auto service status.",
    )
    add_json_argument(status_parser, help_text="Print service status as JSON")
    status_parser.set_defaults(func=cmd_service_status)

    restart_parser = service_subparsers.add_parser(
        "restart",
        help="Restart the orca_auto runtime or engine-worker target.",
    )
    restart_parser.set_defaults(func=cmd_service_restart)


def _orca_auto_version() -> str:
    return package_version()


_EXAMPLES_EPILOG = """\
examples:
  orca_auto init
  orca_auto run-dir /home/user/orca_runs/sample_rxn
  orca_auto queue list --engine orca
  orca_auto queue cancel <target>
  orca_auto service status
"""


def build_parser() -> argparse.ArgumentParser:
    parser = OrcaAutoArgumentParser(
        prog="orca_auto",
        epilog=_EXAMPLES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_orca_auto_version()}",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color in terminal output (also honors NO_COLOR)",
    )
    # Not required: a bare ``orca_auto`` invocation prints help (handled in
    # ``cli.main``) instead of raising an argparse usage error. ``add_subparsers``
    # defaults ``parser_class`` to ``OrcaAutoArgumentParser``, so nested
    # subparsers inherit the styled error handling at runtime; the cast just
    # reconciles the invariant generic with the ``add_*_parser`` helper signatures.
    subparsers = cast(
        "argparse._SubParsersAction[argparse.ArgumentParser]",
        parser.add_subparsers(dest="command", required=False),
    )
    add_queue_parser(subparsers)
    add_run_dir_parser(subparsers)
    add_init_parser(subparsers)
    add_scaffold_parser(subparsers)
    add_systemd_parser(subparsers)
    add_service_parser(subparsers)
    return parser


__all__ = ["OrcaAutoArgumentParser", "build_parser"]
