from __future__ import annotations

from typing import Any


def process_one_xtb_for_test(queue_cmd: Any, cfg: Any) -> str:
    # Mirror the production poll order: read admission capacity, look for a
    # claimable row, and only then reserve. A full pool or an idle queue must
    # not write to admission or list more than it has to.
    if not queue_cmd._ENGINE_RUNTIME.has_admission_capacity(cfg):
        return "blocked"
    if queue_cmd._ENGINE_RUNTIME.peek_next_entry(cfg) is None:
        return "idle"
    slot_token = queue_cmd._try_reserve_admission_slot(cfg)
    if slot_token is None:
        return "blocked"

    try:
        dequeued = queue_cmd.dequeue_next_entry(cfg)
        if dequeued is None:
            return "idle"
        queue_root, entry = dequeued
        queue_cmd._execute_queue_entry(
            cfg,
            queue_root=queue_root,
            entry=entry,
            emit_output=True,
        )
        return "processed"
    finally:
        queue_cmd.release_slot(queue_cmd._admission_root(cfg), slot_token)


def process_one_crest_for_test(queue_cmd: Any, cfg: Any) -> str:
    from orca_auto.flow.engines.crest import execution as crest_worker_execution

    if not queue_cmd._ENGINE_RUNTIME.has_admission_capacity(cfg):
        return "blocked"
    if queue_cmd._ENGINE_RUNTIME.peek_next_entry(cfg) is None:
        return "idle"
    slot_token = queue_cmd._try_reserve_admission_slot(cfg)
    if slot_token is None:
        return "blocked"

    try:
        dequeued = queue_cmd.dequeue_next_entry(cfg)
        if dequeued is None:
            return "idle"
        queue_root, entry = dequeued
        outcome = crest_worker_execution.process_dequeued_entry(
            cfg,
            entry,
            queue_root=queue_root,
            molecule_key_resolver=crest_worker_execution._molecule_key,
            dependencies=queue_cmd._worker_dependencies(),
        )
        print(f"queue_id: {entry.queue_id}")
        print(f"job_id: {entry.task_id}")
        print(f"status: {outcome.result.status}")
        print(f"reason: {outcome.result.reason}")
        return "processed"
    finally:
        queue_cmd.release_slot(queue_cmd._admission_root_for_cfg(cfg), slot_token)
