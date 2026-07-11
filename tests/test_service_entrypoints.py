from __future__ import annotations

from argparse import Namespace

from orca_auto import cli as orca_auto_cli
from orca_auto import cli_workers
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config import MessengerConfig, TelegramConfig
from orca_auto.flow.bot.providers import telegram as telegram_provider
from orca_auto.flow.cli import workflow as cli_workflow
from orca_auto.flow.telegram import bot as telegram_bot


def test_bot_module_main_uses_shared_config(monkeypatch) -> None:
    captured: dict[str, object | None] = {}

    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, "/tmp/orca_auto.yaml")
    monkeypatch.setattr(
        telegram_bot,
        "load_messenger_config_from_file",
        lambda _path: MessengerConfig(provider="discord"),
    )

    def _fake_run_bot(*, config_path=None, provider=None):
        captured["config_path"] = config_path
        captured["provider"] = provider
        return 7

    monkeypatch.setattr(telegram_bot, "_run_neutral_bot", _fake_run_bot)

    result = telegram_bot.main()

    assert result == 7
    assert captured == {
        "config_path": "/tmp/orca_auto.yaml",
        "provider": None,
    }


def test_legacy_bot_entrypoint_preserves_environment_only_telegram_credentials(
    monkeypatch,
) -> None:
    legacy = telegram_bot.TelegramBotSettings(
        telegram=TelegramConfig(bot_token="env-token", chat_id="123"),
        workflow_root="/runs",
        crest_config="/config.yaml",
        xtb_config="/config.yaml",
        orca_config="/config.yaml",
        orca_repo_root="/repo",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        telegram_bot,
        "load_messenger_config_from_file",
        lambda _path: MessengerConfig(provider="telegram"),
    )
    monkeypatch.setattr(telegram_bot, "settings_from_config", lambda _path: legacy)

    def fake_run(application: object, config: TelegramConfig) -> int:
        captured["application"] = application
        captured["config"] = config
        return 9

    monkeypatch.setattr(telegram_provider, "run_telegram_bot", fake_run)

    assert telegram_bot.run_bot() == 9
    assert captured["config"] is legacy.telegram


def test_queue_worker_direct_cli_uses_default_apps(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_cmd_queue_worker(args: Namespace) -> int:
        captured["args"] = args
        return 11

    monkeypatch.setattr(cli_workers, "cmd_queue_worker", _fake_cmd_queue_worker)

    result = orca_auto_cli.main(["queue", "worker", "--config", "/tmp/orca_auto.yaml"])

    assert result == 11
    args = captured["args"]
    assert isinstance(args, Namespace)
    assert args.app is None
    assert args.orca_auto_config == "/tmp/orca_auto.yaml"
    assert args.json is False


def test_workflow_worker_module_main_uses_dedicated_parser(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_cmd_workflow_worker(args: Namespace) -> int:
        captured["args"] = args
        return 17

    monkeypatch.setattr(
        cli_workflow,
        "cmd_workflow_worker",
        _fake_cmd_workflow_worker,
    )

    result = cli_workflow.main(
        [
            "--workflow-root",
            "/tmp/workflows",
            "--orca_auto-config",
            "/tmp/orca_auto.yaml",
            "--once",
        ]
    )

    assert result == 17
    args = captured["args"]
    assert isinstance(args, Namespace)
    assert args.workflow_root == "/tmp/workflows"
    assert args.orca_auto_config == "/tmp/orca_auto.yaml"
    assert args.once is True
