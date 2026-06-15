from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class AttemptRecord(TypedDict, total=False):
    index: int
    inp_path: str
    out_path: str
    return_code: int
    analyzer_status: str
    analyzer_reason: str
    markers: Dict[str, Any]
    patch_actions: List[str]
    started_at: str
    ended_at: str


class RunFinalResult(TypedDict, total=False):
    status: str
    analyzer_status: str
    reason: str
    completed_at: str
    last_out_path: Optional[str]
    resumed: bool
    skipped_execution: bool
    runner_error: str
    telegram_finished_notification_sent_at: str


class RunState(TypedDict, total=False):
    job_id: str
    run_id: str
    reaction_dir: str
    selected_inp: str
    max_retries: int
    max_memory_gb_per_task: int
    status: str
    started_at: str
    updated_at: str
    attempts: List[AttemptRecord]
    final_result: Optional[RunFinalResult]


class RetryNotification(TypedDict):
    reaction_dir: str
    selected_inp: str
    failed_inp: str
    failed_out: str
    next_inp: str
    attempt_index: int
    retry_number: int
    max_retries: int
    analyzer_status: str
    analyzer_reason: str
    patch_actions: List[str]
    resumed: bool


class RunStartedNotification(TypedDict):
    reaction_dir: str
    selected_inp: str
    current_inp: str
    run_id: str
    attempt_index: int
    max_retries: int
    status: str
    attempt_started_at: str
    resumed: bool


class RunFinishedNotification(TypedDict):
    reaction_dir: str
    selected_inp: str
    run_id: str
    status: str
    analyzer_status: str
    reason: str
    attempt_count: int
    max_retries: int
    completed_at: str
    last_out_path: Optional[str]
    resumed: bool
    skipped_execution: bool


class QueueEnqueuedNotification(TypedDict):
    queue_id: str
    reaction_dir: str
    priority: int
    force: bool
    enqueued_at: str
