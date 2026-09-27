"""Rule pin: terminal replay supersession, before and under ``run.lock``.

Whether a pending terminal replay still owns the state file of its reaction
directory is decided once, by ``terminal_marker.terminal_generation_verdict``,
and answered by two readers: ``settlement.is_superseded`` (the pre-check) and
``terminal_state._load_state_for_terminal_generation`` (under ``run.lock``).
They pass it different run ids: the pre-check prefers ``item.run_id``, then
``item.recorded_run_id``, then the observed fingerprint's run id; the
under-lock check reads only the observed fingerprint. The table crosses the
current ``job_state.json`` with replay items whose three run ids disagree and
pins both readers' answers in ``pins/replay_supersession.json``.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

from orca_auto.orca.queue.models import TerminalReplayWorkItem
from orca_auto.orca.queue.settlement import is_superseded
from orca_auto.orca.queue.terminal_marker import StateGenerationFingerprint
from orca_auto.orca.queue.terminal_state import _load_state_for_terminal_generation
from orca_auto.orca.state_reading import state_path
from tests.contracts.normalize import assert_pin

_TASK = "orca-task"
_JOB_IDS = {"equal": _TASK, "different": "orca-other", "empty": ""}
_RUN_IDS = {"equal": "run-A", "different": "run-B", "empty": ""}
_TERMINAL = {"none": None, "set": "completed"}

_OBSERVED: dict[str, StateGenerationFingerprint | None] = {
    "none": None,
    "unreadable": StateGenerationFingerprint(present=True, readable=False),
    "absent": StateGenerationFingerprint(present=False, readable=True),
    "same_task": StateGenerationFingerprint(
        present=True, readable=True, job_id=_TASK, run_id="run-A"
    ),
    "same_task_run_b": StateGenerationFingerprint(
        present=True, readable=True, job_id=_TASK, run_id="run-B"
    ),
    "other_task": StateGenerationFingerprint(
        present=True, readable=True, job_id="orca-other", run_id="run-A"
    ),
}
_ITEM_RUN_IDS: dict[str, str | None] = {"absent": None, "present": "run-A", "different": "run-B"}
_ITEM_RECORDED_RUN_IDS = {"absent": "", "present": "run-A", "different": "run-B"}


def _state_payload(job_id: str, run_id: str, terminal: str | None) -> dict[str, Any]:
    """A normalized ``job_state.json`` with the given generation identity."""
    engine_payload: dict[str, Any] = {"run_id": run_id, "attempts": []}
    if terminal is not None:
        engine_payload["final_result"] = {"status": terminal, "reason": "normal_termination"}
    return {
        "schema_version": 1,
        "engine": "orca",
        "job": {"id": job_id},
        "status": {"state": terminal or "running"},
        "engine_payload": engine_payload,
    }


def _current_states() -> dict[str, str | None]:
    """Current ``job_state.json`` text per row; ``None`` means no file."""
    states: dict[str, str | None] = {"absent": None, "unreadable": "{not json"}
    for (job_name, job_id), (run_name, run_id), (terminal_name, terminal) in itertools.product(
        _JOB_IDS.items(), _RUN_IDS.items(), _TERMINAL.items()
    ):
        key = f"job_id={job_name} run_id={run_name} terminal={terminal_name}"
        states[key] = json.dumps(_state_payload(job_id, run_id, terminal))
    return states


def _under_lock(job_dir: Path, item: TerminalReplayWorkItem) -> str:
    try:
        state = _load_state_for_terminal_generation(
            job_dir, expected_job_id=item.task_id, observed_state=item.observed_state
        )
    except RuntimeError as exc:
        return f"raise RuntimeError: {str(exc).split(':', 1)[0]}"
    return "None" if state is None else "state"


def test_replay_supersession_truth_table(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    table: dict[str, str] = {}
    for state_name, state_text in _current_states().items():
        state_file = state_path(job_dir)
        state_file.unlink(missing_ok=True)
        if state_text is not None:
            state_file.write_text(state_text, encoding="utf-8")
        for (observed_name, observed), (run_name, run_id), (
            recorded_name,
            recorded,
        ) in itertools.product(
            _OBSERVED.items(), _ITEM_RUN_IDS.items(), _ITEM_RECORDED_RUN_IDS.items()
        ):
            item = TerminalReplayWorkItem(
                queue_root=tmp_path,
                queue_id="q-pin",
                reaction_dir=str(job_dir),
                reaction_key=str(job_dir),
                task_id=_TASK,
                observed_status="cancelled",
                selected_inp="",
                error="",
                recorded_run_id=recorded,
                run_id=run_id,
                observed_state=observed,
            )
            key = (
                f"state[{state_name}] observed={observed_name} "
                f"item_run_id={run_name} recorded_run_id={recorded_name}"
            )
            table[key] = (
                f"precheck_superseded={is_superseded(item)} under_lock={_under_lock(job_dir, item)}"
            )
    assert_pin("replay_supersession.json", table)
