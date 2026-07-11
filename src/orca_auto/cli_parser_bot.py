from __future__ import annotations

import argparse

from orca_auto.flow.bot.runner import cmd_bot_run


def add_bot_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    bot_parser = subparsers.add_parser(
        "bot",
        help="Run the configured provider-neutral Telegram or Discord bot.",
    )
    bot_subparsers = bot_parser.add_subparsers(dest="bot_command", required=True)
    run_parser = bot_subparsers.add_parser("run", help="Run the configured messenger gateway.")
    run_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    run_parser.add_argument(
        "--provider",
        choices=("telegram", "discord"),
        default=None,
        help="Override messenger.provider for this process.",
    )
    run_parser.set_defaults(func=cmd_bot_run)


__all__ = ["add_bot_parser"]
