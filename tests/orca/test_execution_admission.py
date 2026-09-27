from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import (
    list_all_slots,
    list_slots,
    prepare_slot_engine_process,
    set_slot_engine_process,
)
from orca_auto.core.admission import persistence as admission_persistence
from orca_auto.core.utils import process as process_utils
from orca_auto.orca import execution
from orca_auto.orca.config import AppConfig
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.queue.worker import _try_reserve_admission_slot
from orca_auto.orca.state_reading import state_path
from tests.conftest import make_run_context


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

    rc = execute_orca_run(make_run_context(cfg, reaction_dir, reaction_dir / "rxn.inp"))

    assert rc == 1
    assert attempts == []
    assert len(list_slots(tmp_path)) == 0
    assert not state_path(reaction_dir).exists()
    # The advisory lock file persists; only kernel ownership is released.
    assert (reaction_dir / "run.lock").exists()


def _slot_rule_outcome(
    tmp_path: Path, cfg: AppConfig, *, activation: str, engine: str, body: str
) -> tuple[str, list[tuple[str, str]]]:
    """Run ``_child_admission_slot`` once; return what it raised and the slots left."""

    job = tmp_path / f"{activation}-{engine}-{body}"
    job.mkdir()
    admission = job / ".admission"
    token = _try_reserve_admission_slot(admission, 1)
    assert token is not None
    if activation == "owner_gone":
        [slot] = list_all_slots(admission)
        stale = replace(slot, process_start_ticks=slot.process_start_ticks + 1)
        admission_persistence.save_slots(admission, [stale])
    context = make_run_context(
        cfg,
        job,
        job / "job.inp",
        admission_root=admission,
        admission_token="" if activation == "no_token" else token,
    )
    raised = "none"
    try:
        with execution._child_admission_slot(context):
            if engine in {"pending", "active"}:
                prepare_slot_engine_process(admission, token)
            if engine == "active":
                ticks = process_utils.current_process_start_ticks()
                assert ticks is not None
                set_slot_engine_process(
                    admission, token, pid=os.getpid(), pgid=os.getpid(), process_start_ticks=ticks
                )
            if body == "raise":
                raise OSError("run failed")
    except Exception as exc:  # noqa: BLE001 - the raised type is the outcome
        raised = type(exc).__name__
    return raised, [(slot.state, slot.engine_process_state) for slot in list_all_slots(admission)]


# (activation, engine state when the run ends, run outcome) -> (raised, slots left).
# The child never releases a slot it activated; the parent releases it.
_SLOT_RULE = {
    ("live", "idle", "return"): ("none", [("active", "idle")]),
    ("live", "pending", "return"): ("none", [("active", "idle")]),
    ("live", "active", "return"): ("ValueError", [("active", "active")]),
    ("live", "idle", "raise"): ("OSError", [("active", "idle")]),
    ("live", "pending", "raise"): ("OSError", [("active", "pending")]),
    ("live", "active", "raise"): ("OSError", [("active", "active")]),
    ("no_token", "idle", "return"): ("AdmissionLimitReachedError", [("reserved", "idle")]),
    ("owner_gone", "idle", "return"): ("AdmissionLimitReachedError", []),
}


def test_child_admission_slot_rule_truth_table(
    tmp_path: Path, app_cfg: Callable[..., AppConfig]
) -> None:
    cfg = app_cfg()

    table = {
        key: _slot_rule_outcome(tmp_path, cfg, activation=key[0], engine=key[1], body=key[2])
        for key in _SLOT_RULE
    }

    assert table == _SLOT_RULE
