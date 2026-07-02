from __future__ import annotations

import time
from typing import Any

from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueWorkerDeps,
    build_internal_engine_queue_worker_deps,
    build_late_bound_internal_engine_queue_worker_deps,
)
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueWorkerFacadeBindings as OrcaQueueWorkerFacadeBindings,
)
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueWorkerFacadeCallbacks as OrcaQueueWorkerFacadeCallbacks,
)


def build_orca_runtime_facade_deps(
    callbacks: OrcaQueueWorkerFacadeCallbacks,
    *,
    time_module: Any = time,
) -> InternalEngineQueueWorkerDeps:
    """Build ORCA queue worker dependencies for the shared engine runtime."""

    return build_internal_engine_queue_worker_deps(callbacks, time_module=time_module)


def build_late_bound_orca_runtime_facade_deps(
    bindings: OrcaQueueWorkerFacadeBindings,
    *,
    time_module: Any = time,
) -> InternalEngineQueueWorkerDeps:
    return build_late_bound_internal_engine_queue_worker_deps(
        bindings,
        time_module=time_module,
    )


__all__ = [
    "OrcaQueueWorkerFacadeBindings",
    "OrcaQueueWorkerFacadeCallbacks",
    "build_late_bound_orca_runtime_facade_deps",
    "build_orca_runtime_facade_deps",
]
