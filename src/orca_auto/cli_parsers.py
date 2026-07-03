from __future__ import annotations

import argparse
from typing import cast

from orca_auto._version import package_version
from orca_auto.cli_argparse import OrcaAutoArgumentParser
from orca_auto.cli_parser_commands import (
    add_init_parser,
    add_monitor_parser,
    add_run_dir_parser,
    add_scaffold_parser,
)
from orca_auto.cli_parser_queue import add_queue_parser
from orca_auto.cli_parser_systemd import add_service_parser, add_systemd_parser


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
    add_monitor_parser(subparsers)
    add_systemd_parser(subparsers)
    add_service_parser(subparsers)
    return parser


__all__ = ["build_parser"]
