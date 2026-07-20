from __future__ import annotations

import argparse

from orca_auto.cli_systemd_apply import cmd_systemd_install
from orca_auto.cli_systemd_status import cmd_service_restart, cmd_service_status
from orca_auto.systemd_plan import DEFAULT_SYSTEMD_UNIT_DIR

from .cli_parser_common import add_json_argument


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
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--unit-dir",
        default=str(DEFAULT_SYSTEMD_UNIT_DIR),
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--worker-only",
        action="store_true",
        help=argparse.SUPPRESS,
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


__all__ = ["add_service_parser", "add_systemd_parser"]
