from __future__ import annotations

import logging
from pathlib import Path

import pytest

from orca_auto.core.messaging.config_io import _load_messenger_config


def test_missing_messenger_config_path_warns_and_disables(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / "messenger.yaml"
    with caplog.at_level(logging.WARNING, logger="orca_auto.core.messaging.config_io"):
        config = _load_messenger_config(missing)
    assert not config.enabled
    assert any("messenger config file not found" in record.message for record in caplog.records)


def test_blank_messenger_config_path_stays_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="orca_auto.core.messaging.config_io"):
        config = _load_messenger_config("")
    assert not config.enabled
    assert caplog.records == []
