"""Common CLI options for engine and workflow workers."""

from __future__ import annotations

import argparse


def add_worker_common_cli_options(
    parser: argparse.ArgumentParser,
    *,
    config_flags: tuple[str, ...] = ("--orca_auto-config", "--config"),
    config_default: str | None = None,
    json_help: str = "Print JSON output",
) -> None:
    parser.add_argument(
        *config_flags,
        dest="orca_auto_config",
        default=config_default,
        help="Path to shared orca_auto.yaml",
    )
    parser.add_argument("--json", action="store_true", help=json_help)
