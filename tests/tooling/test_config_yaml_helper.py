"""``tests.conftest.config_yaml_text`` writes what ``load_config`` reads back.

Integration tests hand an ``AppConfig`` to this shared writer and then exercise
the real loader, so a field the writer drops would silently test a different
configuration than the one the test asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.config import CommonResourceConfig, MessengerConfig
from orca_auto.core.config.schema import DiscordConfig, SlackConfig
from orca_auto.orca.config import load_config
from tests.conftest import make_app_cfg, write_config_file, write_fake_orca

# Obviously synthetic values: no real Slack/Discord workspace, token or channel.
_DISCORD = DiscordConfig(
    bot_token="synthetic-discord-token",
    default_channel_id="123456789012345678",
    timeout_seconds=7.5,
    max_attempts=3,
    retry_backoff_seconds=1.25,
)
_SLACK = SlackConfig(
    bot_token="xoxb-synthetic-helper-token",
    default_channel_id="C0SYNTHETIC",
    timeout_seconds=9.0,
    max_attempts=2,
)

_MESSENGERS = {
    "default": MessengerConfig(),
    "discord-active": MessengerConfig(discord=_DISCORD),
    "discord-timeouts-only": MessengerConfig(discord=DiscordConfig(timeout_seconds=7.5)),
    "slack-active": MessengerConfig(provider="slack", slack=_SLACK),
    "slack-selected-unconfigured": MessengerConfig(provider="slack"),
    "slack-retries-only": MessengerConfig(provider="slack", slack=SlackConfig(max_attempts=3)),
}


@pytest.mark.parametrize("name", sorted(_MESSENGERS))
def test_config_yaml_round_trips_the_selected_messenger(tmp_path: Path, name: str) -> None:
    messenger = _MESSENGERS[name]
    cfg = make_app_cfg(
        tmp_path / "runs",
        orca_executable=write_fake_orca(tmp_path / "orca"),
        max_concurrent=3,
        resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=3),
        messenger=messenger,
    )

    loaded = load_config(str(write_config_file(tmp_path / "orca_auto.yaml", cfg)))

    assert loaded.messenger == messenger
    assert loaded.messenger.enabled is messenger.enabled
    assert loaded.runtime.max_concurrent == 3
    assert loaded.resources == cfg.resources


@pytest.mark.parametrize(
    "messenger",
    [
        MessengerConfig(provider="discord", slack=_SLACK),
        MessengerConfig(provider="slack", discord=_DISCORD, slack=_SLACK),
    ],
    ids=["slack-section-under-discord", "discord-section-under-slack"],
)
def test_config_yaml_refuses_an_unselected_provider_section(
    tmp_path: Path, messenger: MessengerConfig
) -> None:
    cfg = make_app_cfg(tmp_path / "runs", messenger=messenger)

    with pytest.raises(ValueError, match="only the selected provider"):
        write_config_file(tmp_path / "orca_auto.yaml", cfg)
