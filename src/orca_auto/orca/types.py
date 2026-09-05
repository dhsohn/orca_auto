from __future__ import annotations

from typing import Any, TypedDict


class AttemptRecord(TypedDict, total=False):
    index: int
    inp_path: str
    out_path: str
    return_code: int
    analyzer_status: str
    analyzer_reason: str
    markers: dict[str, Any]
    patch_actions: list[str]
    started_at: str
    ended_at: str
    command: list[str]
    input_identity: dict[str, Any]
    executable_identity: dict[str, Any]
    output_identity: dict[str, Any]
    scratch_provenance: dict[str, Any]


class ScratchPublicationRecord(TypedDict, total=False):
    attempt_index: int
    inp_path: str
    outcome: str
    published_at: str
    publication: dict[str, Any]


class RunFinalResult(TypedDict, total=False):
    status: str
    analyzer_status: str
    reason: str
    completed_at: str
    last_out_path: str | None
    resumed: bool
    skipped_execution: bool
    runner_error: str
    finished_notification_sent_at: str


class RunState(TypedDict, total=False):
    job_id: str
    queue_id: str
    queue_generation: str
    run_id: str
    reaction_dir: str
    selected_inp: str
    execution_provenance: dict[str, Any]
    status: str
    started_at: str
    updated_at: str
    attempts: list[AttemptRecord]
    scratch_publications: list[ScratchPublicationRecord]
    final_result: RunFinalResult | None


class RunStartedNotification(TypedDict):
    reaction_dir: str
    selected_inp: str
    current_inp: str
    run_id: str
    attempt_index: int
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
    completed_at: str
    last_out_path: str | None
    resumed: bool
    skipped_execution: bool


class QueueEnqueuedNotification(TypedDict):
    queue_id: str
    reaction_dir: str
    priority: int
    force: bool
    enqueued_at: str
