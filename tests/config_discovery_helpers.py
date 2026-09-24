"""Keep CLI tests away from the shared config that discovery finds on a developer box.

On the canonical box ``~/orca_auto/config/orca_auto.yaml`` is the live robot
config, so a command that falls back to discovery reads it and, through it,
the live worker pid file. Discovery has two fallbacks, ``ORCA_AUTO_CONFIG``
and ``~/orca_auto/config/orca_auto.yaml``; clearing the variable and moving
``HOME`` to an empty directory closes both. An explicit path still resolves;
only the fallbacks go away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR


def isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    empty_home = tmp_path / "no-home"
    empty_home.mkdir(exist_ok=True)
    monkeypatch.delenv(ORCA_AUTO_CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(empty_home))
    return empty_home
