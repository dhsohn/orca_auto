"""A real submission setup shared by the submission and queue tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.config import CommonResourceConfig, MessengerConfig
from orca_auto.orca import submission as run_inp
from orca_auto.orca.queue import notifications as queue_notifications
from tests.conftest import make_app_cfg, write_config_file, write_fake_orca


def real_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    messenger: MessengerConfig | None = None,
) -> tuple[Path, Any]:
    """A real ``orca_auto.yaml`` under ``tmp_path`` that ``load_config`` reads for every call."""

    config = write_config_file(
        tmp_path / "orca_auto.yaml",
        make_app_cfg(
            tmp_path,
            orca_executable=write_fake_orca(tmp_path / "fake_orca", "#!/bin/sh\n"),
            resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=4),
            messenger=messenger,
        ),
    )
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        queue_notifications, "notify_queue_enqueued_event", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(run_inp, "read_worker_pid_file", lambda _root: None)
    args = SimpleNamespace(
        config=str(config),
        path=str(reaction_dir),
        force=False,
        priority=7,
    )
    return reaction_dir, args
