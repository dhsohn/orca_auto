"""Existing-output adoption must not outrank a recorded attempt verdict."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from orca_auto.core.config import ScratchConfig
from orca_auto.orca import execution as run_inp_execution
from orca_auto.orca.config import AppConfig, PathsConfig
from orca_auto.orca.run_context import RunExecutionContext
from orca_auto.orca.state import new_state, save_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.types import AttemptRecord

_COMPLETED_OUT = "****ORCA TERMINATED NORMALLY****\n"


class _RunnerMustNotLaunch:
    def __init__(self, _orca_executable: str) -> None:
        pass

    def run(self, inp_path: Path) -> Any:
        raise AssertionError(f"ORCA must not be launched for {inp_path}")


def _write_generation(
    reaction_dir: Path,
    *,
    status: str,
    attempt: dict[str, Any] | None,
) -> Path:
    reaction_dir.mkdir(parents=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    out = reaction_dir / "rxn.out"
    out.write_text(_COMPLETED_OUT, encoding="utf-8")
    newer = inp.stat().st_mtime_ns + 1_000_000_000
    os.utime(out, ns=(newer, newer))
    state = new_state(reaction_dir, inp)
    state["status"] = status
    if attempt is not None:
        record: dict[str, Any] = {
            "index": 1,
            "inp_path": str(inp),
            "out_path": str(out),
            "patch_actions": [],
            "markers": {},
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:10:00+00:00",
        }
        record.update(attempt)
        state["attempts"].append(cast(AttemptRecord, record))
    save_state(reaction_dir, state)
    return inp


def _execute(
    reaction_dir: Path,
    inp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    @contextmanager
    def passthrough(*_args: object, **_kwargs: object):
        yield

    monkeypatch.setattr(run_inp_execution, "acquire_run_lock", passthrough)
    monkeypatch.setattr(run_inp_execution, "_admission_context", passthrough)
    monkeypatch.setattr(run_inp_execution, "notification_callbacks", lambda _cfg: (None, None))
    context = RunExecutionContext(
        reaction_dir=reaction_dir,
        selected_inp=inp,
        admission_root=reaction_dir.parent / ".admission",
        reservation_token=None,
        admission_app_name=None,
        admission_task_id="",
        cfg=AppConfig(paths=PathsConfig(orca_executable="/bin/true"), scratch=ScratchConfig()),
    )
    return run_inp_execution.execute_locked_run(
        replace(context, force=False), runner_cls=_RunnerMustNotLaunch
    )


def test_recorded_nonzero_exit_outranks_completed_looking_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker died after the attempt record was saved and before the final
    # verdict was: the output carries the completion marker, the record does not.
    reaction_dir = tmp_path / "rxn"
    inp = _write_generation(
        reaction_dir,
        status="running",
        attempt={
            "return_code": 17,
            "analyzer_status": "unknown_failure",
            "analyzer_reason": "nonzero_exit_code",
        },
    )

    exit_code = _execute(reaction_dir, inp, monkeypatch)

    state = load_state(reaction_dir)
    assert state is not None
    assert exit_code == 1
    assert state["status"] == "failed"
    final_result = state["final_result"]
    assert final_result is not None
    assert final_result["reason"] == "nonzero_exit_code"
    assert final_result["analyzer_status"] == "unknown_failure"
    assert "skipped_execution" not in final_result
    assert [attempt["return_code"] for attempt in state["attempts"]] == [17]


def test_settled_failure_is_kept_untouched_instead_of_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A terminal failed state is not resumable, so loading it would start a
    # fresh state and drop the attempt record before adopting the output.
    reaction_dir = tmp_path / "rxn"
    inp = _write_generation(
        reaction_dir,
        status="failed",
        attempt={
            "return_code": 17,
            "analyzer_status": "unknown_failure",
            "analyzer_reason": "nonzero_exit_code",
        },
    )
    settled = load_state(reaction_dir)
    assert settled is not None
    settled["final_result"] = cast(
        Any,
        {
            "status": "failed",
            "reason": "runner_exception",
            "analyzer_status": "incomplete",
            "runner_error": "disk full",
            "finished_notification_sent_at": "2026-01-01T00:11:00+00:00",
        },
    )
    save_state(reaction_dir, settled)
    published = load_state(reaction_dir)
    assert published is not None

    exit_code = _execute(reaction_dir, inp, monkeypatch)

    state = load_state(reaction_dir)
    assert exit_code == 1
    assert state == published


def test_failed_state_without_a_final_result_settles_from_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # load_or_create_state clears a resumable final result as it loads; a crash
    # right after leaves a failed state that is no longer resumable.
    reaction_dir = tmp_path / "rxn"
    inp = _write_generation(
        reaction_dir,
        status="failed",
        attempt={
            "return_code": 17,
            "analyzer_status": "unknown_failure",
            "analyzer_reason": "nonzero_exit_code",
        },
    )
    before = load_state(reaction_dir)
    assert before is not None

    exit_code = _execute(reaction_dir, inp, monkeypatch)

    state = load_state(reaction_dir)
    assert state is not None
    assert exit_code == 1
    assert state["run_id"] == before["run_id"]
    assert state["status"] == "failed"
    final_result = state["final_result"]
    assert final_result is not None
    assert final_result["reason"] == "nonzero_exit_code"
    assert [attempt["return_code"] for attempt in state["attempts"]] == [17]


def test_recorded_successful_attempt_settles_as_an_executed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir = tmp_path / "rxn"
    inp = _write_generation(
        reaction_dir,
        status="running",
        attempt={
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
        },
    )

    exit_code = _execute(reaction_dir, inp, monkeypatch)

    state = load_state(reaction_dir)
    assert state is not None
    assert exit_code == 0
    assert state["status"] == "completed"
    final_result = state["final_result"]
    assert final_result is not None
    assert final_result["reason"] == "normal_termination"
    assert "skipped_execution" not in final_result


@pytest.mark.parametrize("status", ["created", "running"])
def test_output_without_a_recorded_attempt_is_still_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    reaction_dir = tmp_path / "rxn"
    inp = _write_generation(reaction_dir, status=status, attempt=None)
    if status == "running":
        assert run_inp_execution.recover_crashed_state(
            reaction_dir,
            logger=logging.getLogger(__name__),
        )

    exit_code = _execute(reaction_dir, inp, monkeypatch)

    state = load_state(reaction_dir)
    assert state is not None
    assert exit_code == 0
    assert state["status"] == "completed"
    final_result = state["final_result"]
    assert final_result is not None
    assert final_result["reason"] == "existing_out_completed"
    assert final_result["skipped_execution"] is True
    assert state["attempts"] == []
