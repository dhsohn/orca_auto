"""Tests for retry expansion, crash recovery, and structured logging."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from orca_auto.cli import build_parser
from orca_auto.core.admission import reserve_slot
from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.config import load_config
from orca_auto.orca.execution import execute_orca_run
from orca_auto.orca.orca_runner import OrcaRunner
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.run_context import RunExecutionContext, configured_admission_root
from orca_auto.orca.state import (
    RESUMABLE_FAILED_REASONS,
    decide_attempt_outcome,
    is_resumable_state,
)
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import AnalyzerStatus
from orca_auto.orca.types import RunState
from tests.conftest import write_run_state

# ── Retry Strategy Expansion ──


def _analyze(tmp_path: Path, text: str) -> AnalyzerStatus:
    out = tmp_path / "calc.out"
    out.write_text(text, encoding="utf-8")
    result = analyze_output(out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt"))
    return result.status


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("OUT OF MEMORY\n", id="out_of_memory"),
        pytest.param("INSUFFICIENT MEMORY\n", id="insufficient_memory"),
        pytest.param("CANNOT ALLOCATE MEMORY\n", id="cannot_allocate_memory"),
        pytest.param("OUT OF MEMORY\nSCF NOT CONVERGED\n", id="memory_takes_priority_over_scf"),
    ],
)
def test_memory_error_detection(tmp_path: Path, text: str) -> None:
    assert _analyze(tmp_path, text) == AnalyzerStatus.ERROR_MEMORY


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("THE OPTIMIZATION DID NOT CONVERGE\n", id="did_not_converge"),
        pytest.param("OPTIMIZATION HAS NOT YET CONVERGED\n", id="not_yet_converged"),
        pytest.param(
            "OPTIMIZATION HAS NOT YET CONVERGED\n****ORCA TERMINATED NORMALLY****\n",
            id="terminated_normally_still_failed",
        ),
    ],
)
def test_geom_not_converged_detection(tmp_path: Path, text: str) -> None:
    assert _analyze(tmp_path, text) == AnalyzerStatus.GEOM_NOT_CONVERGED


@pytest.mark.parametrize(
    ("analyzer_status", "analyzer_reason"),
    [
        (AnalyzerStatus.ERROR_MEMORY, "out_of_memory"),
        (AnalyzerStatus.GEOM_NOT_CONVERGED, "geometry_not_converged"),
    ],
)
def test_decide_attempt_outcome_is_terminal(
    analyzer_status: AnalyzerStatus, analyzer_reason: str
) -> None:
    result = decide_attempt_outcome(
        analyzer_status=analyzer_status, analyzer_reason=analyzer_reason
    )
    assert result.run_status.value == "failed"


# ── Crash Recovery ──


@pytest.mark.parametrize("reason", ["crashed_recovery", "worker_shutdown"])
def test_reason_is_resumable(reason: str) -> None:
    assert reason in RESUMABLE_FAILED_REASONS
    state: RunState = {"status": "failed", "final_result": {"reason": reason}}
    assert is_resumable_state(state)


def test_crash_recovery_finalizes_recorded_failure_without_rerunning(
    tmp_path: Path,
    config_path: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "orca_runs"
    reaction = runs_root / "rxn_crash"
    reaction.mkdir(parents=True)
    inp = reaction / "rxn.inp"
    inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    config = config_path(runs_root=runs_root)

    # Simulate a crashed run: status=running with a recorded failed attempt
    # and no run.lock holder.
    write_run_state(
        reaction,
        status="running",
        run_id="run_crashed",
        selected_inp=inp,
        attempts=[
            {
                "index": 1,
                "inp_path": str(inp),
                "out_path": str(reaction / "rxn.out"),
                "return_code": 1,
                "analyzer_status": "error_scf",
                "analyzer_reason": "scf_not_converged",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
            }
        ],
    )

    def _no_rerun(_self: OrcaRunner, inp_path: Path) -> None:
        pytest.fail(f"crash recovery must not rerun ORCA on {inp_path}")

    monkeypatch.setattr(OrcaRunner, "run", _no_rerun)
    token = reserve_slot(
        runs_root / ".admission",
        1,
        work_dir=str(reaction),
        source="queue_worker",
        state="reserved",
    )
    cfg = load_config(str(config))
    rc = execute_orca_run(
        RunExecutionContext(
            cfg=cfg,
            reaction_dir=reaction,
            selected_inp=inp,
            admission_root=configured_admission_root(cfg),
            reservation_token=token,
        ),
    )
    saved = load_state(reaction)

    assert rc == 1
    assert saved is not None
    assert saved["run_id"] == "run_crashed"  # Preserved run_id
    assert saved["status"] == "failed"
    final_result = saved["final_result"]
    assert final_result is not None
    assert final_result["resumed"]


def test_log_file_flag_is_accepted() -> None:
    args = build_parser().parse_args(["run-dir", "/tmp/rxn", "--log-file", "/tmp/test.log"])
    assert args.log_file == "/tmp/test.log"
