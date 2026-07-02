from __future__ import annotations

from typing import Any

from orca_auto.core.config import TelegramConfig
from orca_auto.flow.telegram.bot import TelegramBotSettings


def telegram_bot_settings(
    *,
    bot_token: str = "bot-token",
    chat_id: str = "chat-id",
    config_path: str = "/tmp/orca_auto.yaml",
    workflow_root: str | None = "/tmp/workflow_root",
) -> TelegramBotSettings:
    return TelegramBotSettings(
        telegram=TelegramConfig(bot_token=bot_token, chat_id=chat_id),
        workflow_root=workflow_root,
        crest_config=config_path,
        xtb_config=config_path,
        orca_config=config_path,
        orca_repo_root=None,
    )


def workflow_activity(
    activity_id: str,
    *,
    label: str | None = None,
    status: str = "running",
    engine: str = "workflow",
    kind: str = "workflow",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "label": label or activity_id,
        "kind": kind,
        "engine": engine,
        "status": status,
        "source": "orca_auto_flow" if engine == "workflow" else f"orca_auto_{engine}",
        "metadata": dict(metadata or {}),
    }
