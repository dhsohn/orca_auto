from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_workers
from orca_auto.core.config import discovery, engines
from tests.config_discovery_helpers import isolate_shared_config_discovery


def test_seal_reaches_every_binding_of_the_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Plant a config where the home fallback looks, so the test fails without
    # the seal on any machine, not only on one that has a live repository config.
    planted_home = tmp_path / "planted-home"
    planted = planted_home / "orca_auto" / "config" / "orca_auto.yaml"
    planted.parent.mkdir(parents=True)
    planted.write_text("runs_root: /tmp/planted-runs\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(planted_home))
    monkeypatch.setattr(discovery, "repo_root", lambda: tmp_path / "planted-no-repo")
    monkeypatch.setattr(engines, "repo_root", lambda: tmp_path / "planted-no-repo")
    assert discovery.resolve_shared_config_path(None) == str(planted.resolve())

    isolate_shared_config_discovery(monkeypatch, tmp_path)

    assert discovery.resolve_shared_config_path(None) is None
    # cli_workers binds the resolver by from-import; the seal must still reach it.
    assert cli_workers.resolve_shared_config_path(None) is None
    assert discovery.engine_config_for_args(SimpleNamespace()) is None
    assert discovery.workflow_root_for_args(SimpleNamespace()) is None
    # engines bound repo_root at import time; the seal patches that binding too.
    assert not Path(engines.default_shared_config_path()).exists()

    explicit = tmp_path / "explicit.yaml"
    assert discovery.resolve_shared_config_path(str(explicit)) == str(explicit.resolve())
