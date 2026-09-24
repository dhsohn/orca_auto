"""Build the ``orca_auto`` argument parser.

``argparse`` writes its own ``prog: error: ...`` line straight to ``stderr``,
bypassing :mod:`orca_auto.terminal`. :class:`OrcaAutoArgumentParser` funnels
those errors through :func:`orca_auto.terminal.emit_error` so every
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

from orca_auto import cli_handlers, cli_queue, cli_scratch, cli_workers
from orca_auto._version import package_version
from orca_auto.cli_systemd_apply import cmd_systemd_install
from orca_auto.cli_systemd_restart import cmd_service_restart
from orca_auto.cli_systemd_status import cmd_service_status
from orca_auto.systemd_plan import DEFAULT_SYSTEMD_UNIT_DIR
from orca_auto.terminal import emit_error

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


def _non_negative_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--limit must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("--limit must be a non-negative integer")
    return parsed


def add_run_dir_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    run_dir_parser = subparsers.add_parser(
        "run-dir",
        help="Submit an ORCA input directory.",
    )
    add_engine_config_argument(run_dir_parser)
    add_orca_logging_arguments(run_dir_parser)
    run_dir_parser.add_argument("path", help="ORCA input directory")
    run_dir_parser.add_argument(
        "--force",
        action="store_true",
        help=("Force an ORCA re-run"),
    )
    run_dir_parser.add_argument(
        "--priority",
        type=int,
        default=None,
        help="Queue priority when submission is enqueued (lower = higher)",
    )
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


def _add_queue_list_parser(
    queue_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    list_parser = queue_subparsers.add_parser("list", help="List ORCA jobs.")
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
        "--limit",
        type=_non_negative_limit,
        default=0,
        help="Optional non-negative maximum number of activities to print",
    )
    list_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Discover unindexed ORCA runs",
    )
    list_parser.add_argument(
        "--status", action="append", help="Filter by status; may be passed more than once"
    )
    add_json_argument(list_parser)
    list_parser.set_defaults(func=cli_queue.cmd_queue_list)


def _add_queue_cancel_parser(
    queue_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    cancel_parser = queue_subparsers.add_parser("cancel", help="Cancel an ORCA job.")
    cancel_parser.add_argument("target", help="Activity id, queue id, run id, or known path alias")
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
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        default=None,
        help="Path to shared orca_auto.yaml",
    )
    add_json_argument(parser, help_text="Print worker commands as JSON without starting them")


def _add_queue_worker_parser(
    queue_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    worker_parser = queue_subparsers.add_parser("worker", help="Run the unified worker supervisor.")
    _add_queue_worker_options(worker_parser)
    worker_parser.set_defaults(func=cli_workers.cmd_queue_worker)


def add_queue_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    queue_parser = subparsers.add_parser(
        "queue",
        help="ORCA queue and worker commands.",
    )
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command", required=True)
    _add_queue_list_parser(queue_subparsers)
    _add_queue_cancel_parser(queue_subparsers)
    _add_queue_worker_parser(queue_subparsers)


def add_index_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    index_parser = subparsers.add_parser(
        "index",
        help="Maintain the job location index (job_locations.json) in runs_root.",
    )
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)

    prune_parser = index_subparsers.add_parser(
        "prune",
        help=(
            "List, and with --apply remove, index rows whose recorded paths are all gone from disk."
        ),
    )
    prune_parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        help="Path to shared orca_auto.yaml",
    )
    prune_parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the index without the listed rows; nothing is written without it",
    )
    add_json_argument(prune_parser)
    prune_parser.set_defaults(func=cli_handlers.cmd_index_prune)

    rebuild_parser = index_subparsers.add_parser(
        "rebuild",
        help=(
            "Re-derive index rows from every job_state.json under runs_root; "
            "rows are added or updated by job id, never removed."
        ),
    )
    rebuild_parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        help="Path to shared orca_auto.yaml",
    )
    rebuild_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the rows that would be added or updated without writing the index",
    )
    add_json_argument(rebuild_parser)
    rebuild_parser.set_defaults(func=cli_handlers.cmd_index_rebuild)


def add_scratch_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    scratch_parser = subparsers.add_parser(
        "scratch",
        help="Inspect or clear RAM scratch workspaces under orca.runtime.scratch_root.",
    )
    scratch_subparsers = scratch_parser.add_subparsers(dest="scratch_command", required=True)

    list_parser = scratch_subparsers.add_parser(
        "list",
        help="List scratch workspaces and whether they block new scratch launches.",
    )
    list_parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        help="Path to shared orca_auto.yaml",
    )
    add_json_argument(list_parser)
    list_parser.set_defaults(func=cli_scratch.cmd_scratch_list)

    clear_parser = scratch_subparsers.add_parser(
        "clear",
        help=(
            "Remove one named non-live scratch workspace, or every non-live one with "
            "--all-stale; live workspaces are never removed."
        ),
    )
    clear_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Workspace directory name as printed by `scratch list` (attempt-...)",
    )
    clear_parser.add_argument(
        "--all-stale",
        action="store_true",
        help="Remove every stale, unverifiable, or invalid-manifest workspace",
    )
    clear_parser.add_argument(
        "--orca_auto-config",
        "--config",
        dest="orca_auto_config",
        help="Path to shared orca_auto.yaml",
    )
    add_json_argument(clear_parser)
    clear_parser.set_defaults(func=cli_scratch.cmd_scratch_clear)


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
        help="Absolute path to a repository checkout or a prepared wheel runtime",
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
        help="Restart services only when their calculation admission pools are idle.",
    )
    restart_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the idle safety check; running calculations may be interrupted.",
    )
    restart_parser.set_defaults(func=cmd_service_restart)


def _orca_auto_version() -> str:
    return package_version()


_EXAMPLES_EPILOG = """\
examples:
  orca_auto init
  orca_auto run-dir /home/user/orca_runs/sample_rxn
  orca_auto queue list --status running
  orca_auto queue cancel <target>
  orca_auto index prune --apply
  orca_auto index rebuild --dry-run
  orca_auto scratch list
  orca_auto scratch clear attempt-<pid>-<token>
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
    add_index_parser(subparsers)
    add_scratch_parser(subparsers)
    add_systemd_parser(subparsers)
    add_service_parser(subparsers)
    return parser


__all__ = ["OrcaAutoArgumentParser", "build_parser"]
