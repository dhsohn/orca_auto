"""Rule pins for admission root/limit resolution and the child's slot outcome.

``test_admission_resolution`` resolves ``(admission_root, limit)`` from each
config fixture through every consumer that derives it today: ``load_config``,
the queue worker as ``queue worker`` builds it, ``engine_runtime_paths`` and
the systemd unit plan. The table is ``pins/admission_resolution.json``.

``test_child_slot_outcome`` raises each exception type inside the child's
admission context, with the reserved slot's engine process idle, prepared or
registered, and pins the resulting ``admission_slots.json`` in
``pins/admission_child_slot_outcome.json``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from orca_auto import systemd_plan
from orca_auto.core.admission import (
    AdmissionLimitReachedError,
    prepare_slot_engine_process,
    set_slot_engine_process,
)
from orca_auto.core.engine_scratch import EngineScratchCapacityError
from orca_auto.core.utils import process as process_utils
from orca_auto.orca import execution
from orca_auto.orca.config import load_config
from orca_auto.orca.engine_runtime import engine_runtime_paths
from orca_auto.orca.queue import worker as queue_worker
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.run_context import RunExecutionContext
from tests.conftest import make_app_cfg
from tests.contracts.normalize import Normalizer, assert_pin, read_json

# ``{runs}`` and ``{tmp}`` are filled per test; ``{orca}`` names the fake ORCA.
_CONFIGS: dict[str, str] = {
    "no_scheduler": "",
    "scheduler_empty": "scheduler: {{}}\n",
    "max_active_1": "scheduler:\n  max_active_simulations: 1\n",
    "max_active_2": "scheduler:\n  max_active_simulations: 2\n",
    "explicit_root": "scheduler:\n  admission_root: {tmp}/admission\n",
    "explicit_root_max_active_3": (
        "scheduler:\n  admission_root: {tmp}/admission\n  max_active_simulations: 3\n"
    ),
    "explicit_root_equals_default": "scheduler:\n  admission_root: {runs}/.admission\n",
    "explicit_root_unnormalized": "scheduler:\n  admission_root: {tmp}/x/../admission/\n",
    "explicit_root_missing_directory": "scheduler:\n  admission_root: {tmp}/missing\n",
    "explicit_root_relative": "scheduler:\n  admission_root: relative/admission\n",
    "explicit_root_empty": "scheduler:\n  admission_root: ''\n",
}


def _answer(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the raised type is the pinned answer
        return f"raise {type(exc).__name__}: {exc}"


def _load_config_answer(config: Path) -> dict[str, Any]:
    cfg = load_config(str(config))
    return {
        "admission_root": cfg.runtime.resolved_admission_root,
        "admission_limit": cfg.runtime.resolved_admission_limit,
        "max_concurrent": cfg.runtime.max_concurrent,
    }


def _queue_worker_answer(config: Path) -> dict[str, Any]:
    # ``queue worker`` (orca.commands.queue.cmd_queue_worker) builds it this way.
    cfg = load_config(str(config))
    worker = OrcaQueueWorker(cfg, str(config), max_concurrent=max(1, cfg.runtime.max_concurrent))
    return {
        "admission_root": str(worker.admission_root),
        "admission_limit": worker.admission_limit,
        "max_concurrent": worker.max_concurrent,
        "reservation_limit": worker.cfg.runtime.resolved_admission_limit,
    }


def _engine_runtime_answer(config: Path) -> dict[str, Any]:
    return {key: str(value) for key, value in engine_runtime_paths(str(config)).items()}


def _systemd_plan_answer(config: Path) -> dict[str, Any]:
    return {
        "read_write_paths": [
            str(path) for path in systemd_plan._configured_read_write_paths(config)
        ],
        "stop_timeout_seconds": systemd_plan._configured_stop_timeout_seconds(config),
    }


def test_admission_resolution(tmp_path: Path, make_fake_orca: Callable[..., Path]) -> None:
    orca = make_fake_orca()
    runs = tmp_path / "runs"
    runs.mkdir()
    (tmp_path / "admission").mkdir()
    n = Normalizer({tmp_path: "<tmp>"})
    table: dict[str, Any] = {}
    for name, scheduler in _CONFIGS.items():
        config = tmp_path / f"{name}.yaml"
        config.write_text(
            f"runs_root: {runs}\n"
            + scheduler.format(runs=runs, tmp=tmp_path)
            + f"orca:\n  paths:\n    orca_executable: {orca}\n",
            encoding="utf-8",
        )
        table[name] = n(
            {
                "load_config": _answer(partial(_load_config_answer, config)),
                "queue_worker": _answer(partial(_queue_worker_answer, config)),
                "engine_runtime_paths": _answer(partial(_engine_runtime_answer, config)),
                "systemd_plan": _answer(partial(_systemd_plan_answer, config)),
            }
        )
    assert_pin("admission_resolution.json", table)


_CHILD_EXCEPTIONS: dict[str, BaseException | None] = {
    "none": None,
    "EngineScratchCapacityError": EngineScratchCapacityError("scratch root is full"),
    "AdmissionLimitReachedError": AdmissionLimitReachedError("admission limit reached"),
    "RuntimeError": RuntimeError("runtime failure"),
    "other": ValueError("unexpected failure"),
}
_ENGINE_STATES = ("idle", "prepared", "registered")


def _settle_in_admission_context(
    admission: Path, token: str, engine_state: str, raised: BaseException | None
) -> Callable[..., int]:
    """Stand in for the first step inside the activated admission context."""

    def settle(*_args: Any, **_kwargs: Any) -> int:
        if engine_state in {"prepared", "registered"}:
            prepare_slot_engine_process(admission, token)
        if engine_state == "registered":
            ticks = process_utils.current_process_start_ticks()
            assert ticks is not None
            set_slot_engine_process(
                admission, token, pid=os.getpid(), pgid=os.getpid(), process_start_ticks=ticks
            )
        if raised is not None:
            raise raised
        return 0

    return settle


def test_child_slot_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    job = runs / "job"
    job.mkdir(parents=True)
    selected_inp = job / "job.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    table: dict[str, Any] = {}
    for exc_name, raised in _CHILD_EXCEPTIONS.items():
        for engine_state in _ENGINE_STATES:
            admission = tmp_path / f"admission-{exc_name}-{engine_state}"
            cfg = make_app_cfg(runs, admission_root=admission)
            token = queue_worker._try_reserve_admission_slot(cfg)
            assert token is not None

            monkeypatch.setattr(
                execution,
                "existing_completed_exit",
                _settle_in_admission_context(admission, token, engine_state, raised),
            )
            context = RunExecutionContext(
                cfg=cfg,
                reaction_dir=job,
                selected_inp=selected_inp,
                admission_root=admission,
                reservation_token=token,
                admission_app_name="orca_auto",
                admission_task_id="orca-pin",
                queue_id="q-pin",
            )
            result = _answer(partial(execution.execute_orca_run, context))
            slots_path = admission / "admission_slots.json"
            n = Normalizer({tmp_path: "<tmp>"})
            table[f"{exc_name}|{engine_state}"] = n(
                {
                    "result": result,
                    "admission_slots.json": read_json(slots_path) if slots_path.exists() else None,
                }
            )
    assert_pin("admission_child_slot_outcome.json", table)
