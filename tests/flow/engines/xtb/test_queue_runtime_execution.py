from __future__ import annotations

from typing import Any

from orca_auto.flow.engines.xtb.queue_runtime_execution import (
    XtbQueueRuntimeWorkerExecutionCallbacks,
    build_queue_runtime_worker_execution_dependencies,
)


def _callable(name: str, calls: list[str]) -> Any:
    def _call(*_args: Any, **_kwargs: Any) -> str:
        calls.append(name)
        return name

    return _call


def _callbacks(calls: list[str]) -> XtbQueueRuntimeWorkerExecutionCallbacks:
    return XtbQueueRuntimeWorkerExecutionCallbacks(
        activate_reserved_slot=_callable("activate_reserved_slot", calls),
        release_slot=_callable("release_slot", calls),
        load_config=_callable("load_config", calls),
        queue_entry_by_id=_callable("queue_entry_by_id", calls),
        job_dir=_callable("job_dir", calls),
        selected_xyz=_callable("selected_xyz", calls),
        job_type=_callable("job_type", calls),
        reaction_key=_callable("reaction_key", calls),
        input_summary=_callable("input_summary", calls),
        entry_resource_request=_callable("entry_resource_request", calls),
        matching_state=_callable("matching_state", calls),
        is_recovery_pending=_callable("is_recovery_pending", calls),
        write_running_state=_callable("write_running_state", calls),
        build_terminal_result=_callable("build_terminal_result", calls),
        finalize_execution_result=_callable("finalize_execution_result", calls),
        upsert_job_record=_callable("upsert_job_record", calls),
        notify_job_started=_callable("notify_job_started", calls),
        execute_queue_entry=_callable("execute_queue_entry", calls),
        run_xtb_ranking_job=_callable("run_xtb_ranking_job", calls),
        start_xtb_job=_callable("start_xtb_job", calls),
        finalize_xtb_job=_callable("finalize_xtb_job", calls),
        run_path_search_ts_hessian_followup=_callable("run_path_search_ts_hessian_followup", calls),
        terminate_process=_callable("terminate_process", calls),
        wait_for_cancellable_process=_callable("wait", calls),
        sleep=_callable("sleep", calls),
        now_utc_iso=lambda: "2026-01-01T00:00:00+00:00",
        get_cancel_requested=_callable("get_cancel_requested", calls),
        mark_completed=_callable("mark_completed", calls),
        mark_cancelled=_callable("mark_cancelled", calls),
        mark_failed=_callable("mark_failed", calls),
    )


def test_build_worker_execution_dependencies_maps_callback_groups() -> None:
    calls: list[str] = []
    callbacks = _callbacks(calls)

    deps = build_queue_runtime_worker_execution_dependencies(
        callbacks,
        cancel_check_interval_seconds=9,
    )

    assert deps.config.load_config is callbacks.load_config
    assert deps.config.queue_entry_by_id is callbacks.queue_entry_by_id
    assert deps.admission.activate_reserved_slot is callbacks.activate_reserved_slot
    assert deps.admission.release_slot is callbacks.release_slot
    assert deps.context.job_dir is callbacks.job_dir
    assert deps.context.selected_xyz is callbacks.selected_xyz
    assert deps.context.job_type is callbacks.job_type
    assert deps.context.reaction_key is callbacks.reaction_key
    assert deps.context.input_summary is callbacks.input_summary
    assert deps.context.entry_resource_request is callbacks.entry_resource_request
    assert deps.context.matching_state is callbacks.matching_state
    assert deps.context.is_recovery_pending is callbacks.is_recovery_pending
    assert deps.artifacts.write_running_state is callbacks.write_running_state
    assert deps.artifacts.build_terminal_result is callbacks.build_terminal_result
    assert deps.artifacts.finalize_execution_result is callbacks.finalize_execution_result
    assert deps.tracking.upsert_job_record is callbacks.upsert_job_record
    assert deps.tracking.notify_job_started is callbacks.notify_job_started
    assert deps.execute_queue_entry is callbacks.execute_queue_entry
    assert deps.timing.now_utc_iso() == "2026-01-01T00:00:00+00:00"
    assert deps.runner.cancel_check_interval_seconds == 9
    assert deps.runner.run_xtb_ranking_job is callbacks.run_xtb_ranking_job
    assert deps.runner.start_xtb_job is callbacks.start_xtb_job
    assert deps.runner.finalize_xtb_job is callbacks.finalize_xtb_job

    deps.queue.mark_cancelled("root", "queue-1")
    deps.runner.start_xtb_job("cfg", job_dir="job", selected_input_xyz="input.xyz")

    assert calls == ["mark_cancelled", "start_xtb_job"]
