from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_workers
from orca_auto.activity import _cancel
from orca_auto.cli import main
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR, ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.config import discovery, engines
from orca_auto.core.queue.persistence import entry_to_dict
from orca_auto.core.queue.types import QueueEntry, QueueStatus
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


@pytest.mark.parametrize("selected", ["repo", "home", "env", "explicit"])
def test_queue_list_and_cancel_use_the_same_discovered_config(
    selected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = isolate_shared_config_discovery(monkeypatch, tmp_path)
    configs = {
        "repo": repo / "config" / "orca_auto.yaml",
        "home": Path.home() / "orca_auto" / "config" / "orca_auto.yaml",
        "env": tmp_path / "env.yaml",
        "explicit": tmp_path / "explicit.yaml",
    }
    for name, config in configs.items():
        if selected == "home" and name == "repo":
            continue
        runs = tmp_path / f"{name}-runs"
        runs.mkdir()
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f"runs_root: {runs}\n", encoding="utf-8")
        entry = QueueEntry(
            queue_id=f"{name}-job",
            app_name=ORCA_AUTO_ORCA_APP_NAME,
            task_id=f"{name}-task",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.PENDING,
            priority=0,
            enqueued_at="2026-01-01T00:00:00Z",
            metadata={"reaction_dir": str(runs / "job")},
        )
        (runs / "queue.json").write_text(json.dumps([entry_to_dict(entry)]), encoding="utf-8")
    if selected in {"env", "explicit"}:
        monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(configs["env"]))
    options = ["--config", str(configs["explicit"])] if selected == "explicit" else []

    assert main(["queue", "list", "--json", *options]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    target = listed["activities"][0]["activity_id"]
    assert target == f"{selected}-job"
    calls = []

    def cancel_stub(**kwargs):
        calls.append(kwargs)
        return {"status": "cancelled"}

    # Verify discovery and target selection without cancelling or signalling a job.
    monkeypatch.setattr(_cancel, "cancel_orca_target", cancel_stub)
    assert main(["queue", "cancel", target, "--json", *options]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["activity_id"] == target
    assert len(calls) == 1
    assert calls[0]["config_path"] == str(configs[selected])
