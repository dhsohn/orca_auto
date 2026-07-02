from __future__ import annotations

from pathlib import Path

from orca_auto.orca.attempt.reporting import build_final_result
from orca_auto.orca.config import AppConfig, RuntimeConfig
from orca_auto.orca.state import finalize_state, new_state
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus


def make_queue_worker_cfg(tmp: str) -> AppConfig:
    return AppConfig(runtime=RuntimeConfig(allowed_root=tmp))


def write_completed_run_state(reaction_dir: Path) -> None:
    selected_inp = reaction_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    state = new_state(reaction_dir, selected_inp, max_retries=2)
    state["job_id"] = "task_terminal_123"
    state["attempts"].append(
        {
            "index": 1,
            "inp_path": str(selected_inp),
            "out_path": str(reaction_dir / "rxn.out"),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-05-29T12:00:00+00:00",
            "ended_at": "2026-05-29T12:01:00+00:00",
        }
    )
    finalize_state(
        reaction_dir,
        state,
        status=RunStatus.COMPLETED.value,
        final_result=build_final_result(
            status=RunStatus.COMPLETED,
            analyzer_status=AnalyzerStatus.COMPLETED,
            reason="normal_termination",
            last_out_path=str(reaction_dir / "rxn.out"),
            resumed=False,
        ),
    )


__all__ = ["make_queue_worker_cfg", "write_completed_run_state"]
