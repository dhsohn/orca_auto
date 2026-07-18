from __future__ import annotations

from orca_auto.core.config.engines import load_xtb_config
from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
)
from orca_auto.core.notifications.engines import (
    notify_xtb_job_finished,
    notify_xtb_job_started,
)

from .job_locations import runtime_roots_for_cfg

ENGINE_DEFINITION = build_queue_engine_definition(
    engine="xtb",
    load_config=load_xtb_config,
    run_worker_child_job=build_lazy_worker_child_runner(
        "orca_auto.flow.engines.xtb.execution",
        "run_worker_job",
    ),
    queue_worker_runner=build_lazy_queue_worker_runner("orca_auto.flow.engines.xtb.queue_runtime"),
    worker_pid_file_name="xtb_queue_worker.pid",
    runtime_roots_for_cfg=runtime_roots_for_cfg,
    job_started=notify_xtb_job_started,
    job_finished=notify_xtb_job_finished,
)
build_worker_child_command = ENGINE_DEFINITION.runner_callbacks.build_worker_child_command


__all__ = ["ENGINE_DEFINITION", "build_worker_child_command"]
