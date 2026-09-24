from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import active_slot_count
from orca_auto.orca import execution
from orca_auto.orca.config import AppConfig
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.run_context import RunExecutionContext, configured_admission_root
from orca_auto.orca.state_reading import state_path


def test_internal_run_rejects_without_queue_reservation(
    tmp_path: Path,
    app_cfg: Callable[..., AppConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[Any, ...]] = []

    def record_attempts(*args: Any, **_kwargs: Any) -> int:
        attempts.append(args)
        return 0

    monkeypatch.setattr(execution, "run_attempts", record_attempts)
    cfg = app_cfg(max_concurrent=1)
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
    )

    rc = execute_orca_run(
        RunExecutionContext(
            cfg=cfg,
            reaction_dir=reaction_dir,
            selected_inp=reaction_dir / "rxn.inp",
            admission_root=configured_admission_root(cfg),
        )
    )

    assert rc == 1
    assert attempts == []
    assert active_slot_count(tmp_path) == 0
    assert not state_path(reaction_dir).exists()
    # The advisory lock file persists; only kernel ownership is released.
    assert (reaction_dir / "run.lock").exists()
