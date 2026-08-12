from __future__ import annotations

from orca_auto.core.config.engines import load_crest_config
from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
)
from orca_auto.core.notifications.engines import (
    notify_crest_job_finished,
    notify_crest_job_started,
)

ENGINE_DEFINITION = build_queue_engine_definition(
    engine="crest",
    load_config=load_crest_config,
    run_worker_child_job=build_lazy_worker_child_runner(
        "orca_auto.flow.engines.crest.execution",
        "run_worker_child_job",
    ),
    queue_worker_runner=build_lazy_queue_worker_runner(
        "orca_auto.flow.engines.crest.queue_runtime"
    ),
    worker_pid_file_name="crest_queue_worker.pid",
    job_started=notify_crest_job_started,
    job_finished=notify_crest_job_finished,
)
build_worker_child_command = ENGINE_DEFINITION.runner_callbacks.build_worker_child_command


__all__ = ["ENGINE_DEFINITION", "build_worker_child_command"]
