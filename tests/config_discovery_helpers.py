"""Keep CLI tests away from the shared config that discovery finds on a developer box.

On the canonical checkout ``config/orca_auto.yaml`` exists and the editable
install makes ``core.config.discovery.repo_root()`` point at that checkout, so a
command that falls back to discovery reads the live robot config and, through
it, the live worker pid file. CI checks out only the ``.example`` template, so
the same test passes there whatever it read. Discovery has three fallbacks —
``ORCA_AUTO_CONFIG``, ``<repo_root>/config/orca_auto.yaml`` and
``~/orca_auto/config/orca_auto.yaml`` — and on the canonical box the last two
are the same file. Clearing the variable, moving ``HOME`` to an empty directory
and pointing ``repo_root`` at another closes all three. ``repo_root`` is bound
in two modules — ``discovery`` reads it at call time, ``engines`` imported the
name — so both are patched. An explicit path still resolves; only the
fallbacks go away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config import discovery, engines


def isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    empty_root = tmp_path / "no-repo"
    empty_home = tmp_path / "no-home"
    empty_root.mkdir(exist_ok=True)
    empty_home.mkdir(exist_ok=True)
    monkeypatch.delenv(ORCA_AUTO_CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setattr(discovery, "repo_root", lambda: empty_root)
    monkeypatch.setattr(engines, "repo_root", lambda: empty_root)
    return empty_root
