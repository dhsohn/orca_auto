from __future__ import annotations

import argparse


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


__all__ = [
    "add_engine_config_argument",
    "add_json_argument",
    "add_orca_logging_arguments",
    "add_resource_override_arguments",
]
