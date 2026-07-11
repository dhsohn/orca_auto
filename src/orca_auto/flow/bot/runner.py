"""Provider-neutral process entrypoint for the interactive orca_auto bot."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config.files import config_env_value
from orca_auto.core.messaging import load_required_messenger_config_from_file

from .application import BotApplication
from .settings import settings_from_config

LOGGER = logging.getLogger(__name__)


def run_bot(*, config_path: str | None = None, provider: str | None = None) -> int:
    requested_path = config_path or config_env_value(ORCA_AUTO_CONFIG_ENV_VAR) or None
    settings = settings_from_config(requested_path)
    resolved_path = requested_path or settings.orca_config
    messenger_config = load_required_messenger_config_from_file(resolved_path)
    selected = (provider or messenger_config.normalized_provider).strip().lower()
    if selected == "telegram":
        from .providers.telegram import run_telegram_bot

        application = BotApplication(settings)
        return run_telegram_bot(application, messenger_config.telegram)
    if selected == "discord":
        from .providers.discord import run_discord_bot

        application = BotApplication(
            settings,
            upload_policy=messenger_config.discord.uploads,
        )
        return run_discord_bot(application, messenger_config.discord)
    raise ValueError(f"unsupported messenger provider: {selected!r}")


def cmd_bot_run(args: Any) -> int:
    try:
        return run_bot(
            config_path=str(getattr(args, "config", "") or "").strip() or None,
            provider=str(getattr(args, "provider", "") or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001 - stable CLI boundary for provider SDK failures
        LOGGER.error("bot_start_failed: %s: %s", type(exc).__name__, exc)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.bot.runner")
    parser.add_argument("--config", default=None)
    parser.add_argument("--provider", choices=("telegram", "discord"), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    return cmd_bot_run(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["cmd_bot_run", "main", "run_bot"]
